"""``trackmend lyrics``: add missing lyrics from LRCLIB, a few tracks at a time.

Lyrics are looked up with the artist and the title, so they come last: after ``tags``
has fixed the names. When ``tags`` (or ``review``) changes the names of a track whose
lyrics were not found before, its lyrics are looked up again straight away, and this
command also looks again at any track whose names changed since its last search.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Iterable, List, Optional, Tuple

from . import text
from .lrclib import LrcLib, Lyrics
from .music import Track
from .net import NetError
from .ops import Context, local_file, shorten
from .scan import NOT_MUSIC

MIN_LEN, MAX_LEN = 30, 15 * 60
BATCH = 10
SETTLED = {"written", "instrumental", "not-found"}   # "retry": LRCLIB was busy, look again
NAME_FIELDS = {"title", "artist", "album"}


def candidates(tracks: Iterable[Track]) -> List[Track]:
    out = []
    for t in tracks:
        if not local_file(t) or t.lyrics_len or not (MIN_LEN <= t.duration <= MAX_LEN):
            continue
        if NOT_MUSIC.search(t.name) or NOT_MUSIC.search(os.path.basename(t.location or "")):
            continue
        if "instrumental" in text.parse(t.name, t.artist).qualifiers:
            continue
        out.append(t)
    return sorted(out, key=lambda t: (t.artist.lower(), t.name.lower()))


def search_key(t: Track) -> str:
    """What a search depended on: when it changes, the search is made again."""
    return "|".join((text.compact(t.artist), text.compact(t.name), text.compact(t.album),
                     str(int(round(t.duration or 0)))))


def to_look_up(tracks: Iterable[Track], seen: Dict[str, Tuple[str, str]], retry: bool = False) -> List[Track]:
    settled = SETTLED - ({"not-found"} if retry else set())
    out = []
    for t in candidates(tracks):
        outcome, key = seen.get(t.pid, ("", ""))
        if outcome in settled and not (key and key != search_key(t)):
            continue
        out.append(t)
    return out


def search(lrc: LrcLib, t: Track) -> Tuple[Track, Optional[Lyrics], str]:
    try:
        return t, lrc.find(text.parse(t.name, t.artist), t.album, t.duration), ""
    except NetError as e:
        return t, None, str(e)


class Writer:
    """Writes lyrics into Music in small batches, journal first."""

    def __init__(self, ctx: Context, run, progress):
        self.ctx, self.run, self.progress = ctx, run, progress
        self.pending: List[Tuple[Track, Lyrics]] = []
        self.written = 0

    batch = BATCH

    def add(self, t: Track, found: Lyrics) -> None:
        self.pending.append((t, found))
        if len(self.pending) >= self.batch:
            self.flush()

    def flush(self) -> None:
        if not self.pending:
            return
        batch, self.pending = self.pending, []
        seqs: Dict[str, int] = {}
        for t, found in batch:
            seqs[t.pid] = self.run.begin("lyrics", pid=t.pid, label=t.label(), before=self.ctx.music.lyrics(t.pid),
                                         lrclib_id=found.lrclib_id, length=len(found.text))
        errors = self.ctx.music.set({t.pid: {"lyrics": found.text} for t, found in batch})
        failed = {e.split(":", 1)[0] for e in errors}
        for t, _ in batch:
            if t.pid in failed:
                self.progress.set(t.pid, "error", "Music refused the lyrics")
                continue
            self.run.done(seqs[t.pid])
            self.progress.set(t.pid, "written", search_key(t))
            self.written += 1


def look_up(ctx: Context, todo: List[Track], progress, writer: Optional[Writer]) -> Dict[str, int]:
    """Search LRCLIB for each track; write what is found when ``writer`` is given.

    LRCLIB answers 503 at random when it is overloaded. Each request is tried again
    quickly (see net.py); tracks that still failed get one more pass at the end, and
    the ones left are noted "to redo" (never "not found"), so the next run tries them.
    """
    apply = writer is not None
    counts = {"found": 0, "missing": 0, "redo": 0}
    lrc = LrcLib(ctx.http())
    batch, failed = list(todo), []
    for attempt in (1, 2):
        pool = ThreadPoolExecutor(max_workers=3)
        futures: list = []
        try:
            futures = [pool.submit(search, lrc, t) for t in batch]
            for k, fut in enumerate(as_completed(futures), 1):
                t, found, err = fut.result()
                if err:
                    failed.append((t, err))
                elif found and found.instrumental:
                    counts["missing"] += 1
                    if apply:
                        progress.set(t.pid, "instrumental", search_key(t))
                elif found:
                    counts["found"] += 1
                    how = "same song" if found.exact else "close title"
                    ctx.say(f"  + {shorten(t.label(), 60)}  ({how}, ±{int(found.gap)} s)")
                    if writer:
                        writer.add(t, found)
                else:
                    counts["missing"] += 1
                    if apply:
                        progress.set(t.pid, "not-found", search_key(t))
                if k % 100 == 0:
                    ctx.say(f"  ... {k}/{len(batch)} looked up, {counts['found']} found")
        except KeyboardInterrupt:
            for f in futures:
                f.cancel()
            raise
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if not failed or attempt == 2:
            break
        ctx.say(f"  LRCLIB was busy for {len(failed)} tracks; trying them once more")
        batch, failed = [t for t, _ in failed], []
    for t, err in failed:
        counts["redo"] += 1
        ctx.say(f"  ~ {shorten(t.label(), 60)}: to redo ({err})")
        if apply:
            progress.set(t.pid, "retry", search_key(t))
    return counts


def _summary(ctx: Context, counts: Dict[str, int], writer: Optional[Writer]) -> str:
    redo = f", {counts['redo']} to redo (LRCLIB was busy: run the command again)" if counts["redo"] else ""
    if writer:
        return f"{writer.written} lyrics added, {counts['missing']} not found{redo}."
    return f"{counts['found']} lyrics found, {counts['missing']} not found{redo}."


def run(ctx: Context, tracks: List[Track], args, argv: List[str]) -> int:
    progress = ctx.home.progress("lyrics")
    if args.track:
        from .split import find_track
        t = find_track(tracks, args.track)
        if t.lyrics_len and not args.redo:
            ctx.say(f"{t.label()} already has lyrics; add --redo to replace them.")
            return 0
        todo = [t]
    else:
        todo = to_look_up(tracks, {} if args.redo else progress.entries(), retry=args.retry)
    if args.limit:
        todo = todo[: args.limit]
    ctx.say(f"{len(todo)} tracks without lyrics to look up" + ("" if args.apply else " (dry run)"))
    _tags_first_hint(ctx, todo)
    run_ = ctx.home.new_run("lyrics", argv) if args.apply else None
    writer = Writer(ctx, run_, progress) if run_ else None
    if writer and args.track:
        writer.batch = 1  # one track: write it right away
    counts = {"found": 0, "missing": 0, "redo": 0}
    try:
        counts = look_up(ctx, todo, progress, writer)
    except KeyboardInterrupt:
        ctx.say("\nStopped. Lyrics written so far are saved; run the same command to continue.")
    finally:
        if writer:
            writer.flush()
        progress.close()
    ctx.say("")
    ctx.say(_summary(ctx, counts, writer))
    if writer:
        ctx.say(f"Undo with: trackmend undo {run_.id} --apply")
    elif counts["found"]:
        ctx.say("Nothing was changed. Run again with --apply to add them.")
    return 0


def _tags_first_hint(ctx: Context, todo: List[Track]) -> None:
    """Lyrics are found with the artist and title: say so when those still look raw."""
    done = ctx.home.progress("tags")
    handled = done.get()
    done.close()
    raw = [t for t in todo if t.pid not in handled and text.needs_cleanup(t.name, t.artist)]
    if raw:
        ctx.say(f"  {len(raw)} of them still have names to fix: run `trackmend tags --apply` first. "
                "Their lyrics are looked up again when tags changes them.")


def changed_names(changes: Dict) -> bool:
    return bool(NAME_FIELDS & set(changes))


def search_again(ctx: Context, pids: Iterable[str], source: str = "tags", apply: bool = True) -> None:
    """After ``tags`` or ``review`` changed the names of some tracks: look their lyrics up
    again when an earlier search (made with the old names) found none."""
    progress = ctx.home.progress("lyrics")
    try:
        seen = progress.entries()
        pids = [p for p in dict.fromkeys(pids) if p in seen and seen[p][0] != "written"]
        if not pids:
            return
        if not apply:
            ctx.say(f"Lyrics will be looked up again for {len(pids)} of these tracks "
                    "(none were found with their old names).")
            return
        info = ctx.music.info(pids)
        tracks = [Track.from_dict({**i, "lyrics_len": len(i.get("lyrics") or "")}) for i in info.values() if i]
        todo = [t for t in candidates(tracks) if seen[t.pid][1] != search_key(t)]
        if not todo:
            return
        ctx.say(f"\nLyrics: {len(todo)} tracks had none with their old names; looking again")
        run_ = ctx.home.new_run("lyrics", ["lyrics", "--apply", f"(after {source})"])
        writer = Writer(ctx, run_, progress)
        counts = {"found": 0, "missing": 0, "redo": 0}
        try:
            counts = look_up(ctx, todo, progress, writer)
        finally:
            writer.flush()
        ctx.say(_summary(ctx, counts, writer))
        if writer.written:
            ctx.say(f"Undo the lyrics with: trackmend undo {run_.id} --apply")
    finally:
        progress.close()
