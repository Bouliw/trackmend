"""``trackmend review``: look at doubtful matches before anything is written.

``tags --apply`` writes only what it is sure of. Matches it cannot confirm (artist written
in another script, title-only matches, YouTube thumbnails as artwork) wait here. This
command lists them, writes a page with the old and new artwork side by side, and writes
the ones you accept.
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import time
from typing import Dict, List, Optional, Sequence

from . import audio, lyrics
from .music import Track
from .ops import Context, Stop, Syncer, shorten, write_tags


class ReviewStore:
    """Pending decisions, kept in ``review/review.json`` with the proposed artwork next to it."""

    def __init__(self, root: str):
        self.dir = os.path.join(root, "review")
        self.path = os.path.join(self.dir, "review.json")
        self.items: Dict[str, Dict] = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                self.items = json.load(fh)

    def save(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.items, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def add(self, track: Track, changes: Dict, cover: Optional[bytes], cover_source: str,
            reason: str, source: str, before: Optional[Dict] = None) -> None:
        os.makedirs(self.dir, exist_ok=True)
        cover_file = ""
        if cover:
            cover_file = os.path.join(self.dir, f"{track.pid}.new" + (".png" if cover[:4] == b"\x89PNG" else ".jpg"))
            with open(cover_file, "wb") as fh:
                fh.write(cover)
        self.items[track.pid] = {
            "pid": track.pid, "label": track.label(), "old": {"title": track.name, "artist": track.artist,
                                                             "album": track.album},
            "changes": changes, "before": before or {}, "cover_file": cover_file, "cover_source": cover_source,
            "reason": reason, "source": source, "status": "pending", "time": time.time(),
        }
        self.save()

    def ordered(self) -> List[Dict]:
        return sorted(self.items.values(), key=lambda i: (i["status"] != "pending", i["label"].lower()))


def pick_numbers(spec: str, count: int) -> List[int]:
    """``"1,3-5"`` / ``"all"`` -> [0, 2, 3, 4] (0-based)."""
    if spec.strip().lower() == "all":
        return list(range(count))
    out = set()
    for part in re.split(r"[,\s]+", spec.strip()):
        if not part:
            continue
        a, _, b = part.partition("-")
        if not a.isdigit() or (b and not b.isdigit()):
            raise Stop(f"not a list of numbers: {spec}")
        lo, hi = int(a), int(b or a)
        out.update(i - 1 for i in range(lo, hi + 1) if 1 <= i <= count)
    return sorted(out)


def describe(item: Dict) -> str:
    c, old = item["changes"], item["old"]
    new_artist = c.get("artist", old.get("artist", ""))
    new_title = c.get("title", old.get("title", ""))
    parts = []
    if c:
        parts.append(f"{new_artist} - {new_title}" + (f"  [{c['album']}]" if c.get("album") else ""))
    if item.get("cover_file"):
        parts.append(f"artwork: {item['cover_source']}")
    return "; ".join(parts)


def write_page(ctx: Context, store: ReviewStore, items: List[Dict]) -> str:
    """An HTML page with each doubtful match and its artwork, old and new."""
    cards = []
    for n, item in enumerate(items, 1):
        old_img = ""
        loc = ctx.music.location(item["pid"])
        if loc and os.path.exists(loc):
            try:
                data = audio.cover_bytes(loc)
            except audio.AudioError:
                data = None
            if data:
                old_img = os.path.join(store.dir, f"{item['pid']}.old" + (".png" if data[:4] == b"\x89PNG" else ".jpg"))
                with open(old_img, "wb") as fh:
                    fh.write(data)
        new_img = item.get("cover_file") or ""

        def img(path: str, label: str) -> str:
            if not path:
                return f'<div class="none">{label}<br>none</div>'
            return f'<figure><img src="{html.escape(os.path.basename(path))}"><figcaption>{label}</figcaption></figure>'

        why = " \u00b7 ".join(x for x in (item["reason"], item.get("source"), item["status"]) if x)
        cards.append(f"""<section class="{item['status']}"><h2>{n}. {html.escape(item['label'])}</h2>
<p class="new">→ {html.escape(describe(item)) or '(artwork only)'}</p>
<p class="why">{html.escape(why)}</p>
<div class="art">{img(old_img, 'now')}{img(new_img, 'proposed')}</div></section>""")
    page = f"""<!doctype html><meta charset="utf-8"><title>trackmend review</title><style>
body {{ font: 14px -apple-system, Helvetica, sans-serif; background: #f4f2ee; color: #222; margin: 24px; }}
section {{ background: #fff; border-radius: 10px; padding: 14px 18px; margin: 0 0 14px; max-width: 760px; }}
section.accepted {{ border-left: 6px solid #3a9a5b; }} section.rejected {{ opacity: .5; }}
h2 {{ font-size: 15px; margin: 0 0 6px; }} .new {{ margin: 0; }} .why {{ color: #777; margin: 4px 0 10px; }}
.art {{ display: flex; gap: 16px; }} figure {{ margin: 0; text-align: center; color: #777; }}
img {{ width: 220px; height: 220px; object-fit: contain; background: #eee; border-radius: 6px; }}
.none {{ width: 220px; height: 220px; background: #eee; border-radius: 6px; display: flex; align-items: center;
        justify-content: center; text-align: center; color: #999; }}
</style><h1>Doubtful matches</h1>
<p>Accept with <code>trackmend review --accept 1,3</code>, reject with <code>--reject 2</code>,
then write the accepted ones with <code>trackmend review --apply</code>.</p>
{''.join(cards)}"""
    path = os.path.join(store.dir, "review.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return path


def run(ctx: Context, args, argv: List[str]) -> int:
    store = ReviewStore(ctx.home.root)
    items = [i for i in store.ordered() if i["status"] in ("pending", "accepted")]
    if not items:
        ctx.say("Nothing to review.")
        return 0
    if args.accept or args.reject:
        for spec, status in ((args.accept, "accepted"), (args.reject, "rejected")):
            if spec:
                for k in pick_numbers(spec, len(items)):
                    store.items[items[k]["pid"]]["status"] = status
        store.save()
        items = [i for i in store.ordered() if i["status"] in ("pending", "accepted")]
    elif not args.apply and _interactive(ctx):
        ask(ctx, store, items)
        items = [i for i in store.ordered() if i["status"] in ("pending", "accepted")]

    ctx.say(f"{len(items)} doubtful matches:")
    for n, item in enumerate(items, 1):
        mark = "✓" if item["status"] == "accepted" else " "
        ctx.say(f" {mark}{n:>3}. {shorten(item['label'], 60)}")
        ctx.say(f"        → {shorten(describe(item), 80)}")
        ctx.say(f"        {item['reason']}")
    page = write_page(ctx, store, items)
    ctx.say(f"\nArtwork side by side: {page}")
    if args.open and sys.platform == "darwin":
        subprocess.run(["open", page])
    if not args.apply:
        ctx.say("Nothing was changed. Accept with --accept 1,3 (or all), then write with --apply.")
        return 0
    return apply(ctx, store, argv, no_lyrics=getattr(args, "no_lyrics", False))


def _interactive(ctx: Context) -> bool:
    return sys.stdin.isatty() if ctx.interactive is None else ctx.interactive


def ask(ctx: Context, store: ReviewStore, items: Sequence[Dict]) -> None:
    for item in items:
        if item["status"] != "pending":
            continue
        ctx.say(f"\n{item['label']}\n  → {describe(item)}\n  {item['reason']}")
        try:
            answer = input("  Accept? [y]es / [n]o / [s]kip / [q]uit: ").strip().lower()
        except EOFError:
            break
        if answer.startswith("q"):
            break
        if answer.startswith("y"):
            item["status"] = "accepted"
        elif answer.startswith("n"):
            item["status"] = "rejected"
    store.save()


def apply(ctx: Context, store: ReviewStore, argv: List[str], no_lyrics: bool = False) -> int:
    accepted = [i for i in store.ordered() if i["status"] == "accepted"]
    rejected = [i for i in store.ordered() if i["status"] == "rejected"]
    progress = ctx.home.progress("tags")
    for item in rejected:
        progress.set(item["pid"], "rejected", item["reason"])
        store.items.pop(item["pid"], None)
    if not accepted:
        store.save()
        progress.close()
        ctx.say("Nothing accepted yet.")
        return 0
    tracks = {t.pid: t for t in ctx.music.tracks()}
    run_ = ctx.home.new_run("review", argv)
    syncer = Syncer(ctx, progress)
    written = 0
    renamed: List[str] = []
    for item in accepted:
        t = tracks.get(item["pid"])
        if t is None:
            ctx.say(f"  ! {item['label']}: no longer in Music")
            store.items.pop(item["pid"], None)
            continue
        cover = None
        if item.get("cover_file") and os.path.exists(item["cover_file"]):
            with open(item["cover_file"], "rb") as fh:
                cover = fh.read()
        before = item.get("before") or {}
        try:
            write_tags(ctx, run_, t, item["changes"], cover,
                       before={k: before.get(k, "") for k in item["changes"]})
        except (Stop, audio.AudioError, OSError) as e:
            ctx.say(f"  ! {item['label']}: {e}")
            continue
        progress.set(t.pid, "written", "reviewed", synced=False)
        syncer.add(t.pid, artwork="show" if cover else "")
        store.items.pop(item["pid"], None)
        written += 1
        if lyrics.changed_names(item["changes"]):
            renamed.append(t.pid)
    syncer.flush()
    store.save()
    progress.close()
    ctx.say(f"{written} reviewed matches written. Undo with: trackmend undo {run_.id} --apply")
    if renamed and not no_lyrics:
        lyrics.search_again(ctx, renamed, "review")
    return 0
