"""``trackmend dedup``: the same song twice (same artist and title, duration within 4 s)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from . import text
from .music import Track
from .ops import Context, Stop, fmt_duration, local_file, remove_track, shorten

GAP = 4.0


@dataclass
class Group:
    keep: Track
    remove: List[Track]


@dataclass
class Duplicates:
    groups: List[Group] = field(default_factory=list)
    maybe: List[Tuple[Track, Track]] = field(default_factory=list)


def song_key(t: Track) -> Tuple[str, str, Tuple[str, ...]]:
    p = text.parse(t.name, t.artist)
    artist = text.compact(text.primary_artist(p.artist))
    return artist, p.core, tuple(sorted(p.qualifiers))


def best_first(t: Track) -> tuple:
    """Favorite first, then most played, then with lyrics, then best bit rate, then oldest."""
    return (not t.favorited, -t.plays, t.lyrics_len == 0, -t.bit_rate, t.date_added or "9999")


def find_duplicates(tracks: List[Track]) -> Duplicates:
    by_key: Dict[tuple, List[Track]] = defaultdict(list)
    by_title: Dict[tuple, List[Tuple[str, Track]]] = defaultdict(list)
    for t in tracks:
        artist, core, quals = song_key(t)
        if not core:
            continue
        if artist:
            by_key[(artist, core, quals)].append(t)
        by_title[(core, quals)].append((artist, t))
    out = Duplicates()
    taken = set()
    for ts in by_key.values():
        if len(ts) < 2:
            continue
        ts = sorted(ts, key=best_first)
        keep, extra = ts[0], [t for t in ts[1:] if abs(t.duration - ts[0].duration) <= GAP]
        if extra:
            out.groups.append(Group(keep, extra))
            taken.update(t.pid for t in extra)
    for (core, _), items in by_title.items():
        if len(items) < 2 or len(core) < 3:
            continue
        for i, (a1, t1) in enumerate(items):
            for a2, t2 in items[i + 1:]:
                if t1.pid in taken or t2.pid in taken or abs(t1.duration - t2.duration) > GAP + 1:
                    continue
                if (a1 and a2) and a1 == a2:
                    continue  # already handled above
                if not a1 or not a2:
                    out.maybe.append((t1, t2))
    out.groups.sort(key=lambda g: g.keep.label().lower())
    return out


def run(ctx: Context, tracks: List[Track], apply: bool, argv: List[str]) -> int:
    songs = [t for t in tracks if local_file(t)]
    d = find_duplicates(songs)
    if not d.groups and not d.maybe:
        ctx.say("No duplicates found.")
        return 0
    extra = sum(len(g.remove) for g in d.groups)
    ctx.say(f"{len(d.groups)} songs have duplicates ({extra} extra copies):")
    for g in d.groups:
        k = g.keep
        ctx.say(f"  keep    {shorten(k.label(), 60)}  [{fmt_duration(k.duration)}, {k.plays} plays, "
                f"{k.bit_rate} kbps{', lyrics' if k.lyrics_len else ''}]")
        for t in g.remove:
            ctx.say(f"  remove  {shorten(t.label(), 60)}  [{fmt_duration(t.duration)}, {t.plays} plays, "
                    f"{t.bit_rate} kbps{', lyrics' if t.lyrics_len else ''}]")
    if d.maybe:
        ctx.say("")
        ctx.say(f"{len(d.maybe)} possible duplicates to check by hand (one copy has no artist):")
        for a, b in d.maybe:
            ctx.say(f"  {shorten(a.label(), 45)}  <->  {shorten(b.label(), 45)}")
    if not apply:
        ctx.say("")
        ctx.say("Nothing was changed. Run again with --apply to remove the extra copies.")
        return 0
    if not extra or not ctx.confirm(f"Remove {extra} extra copies from Music?"):
        return 0
    run_ = ctx.home.new_run("dedup", argv)
    done = 0
    for g in d.groups:
        for t in g.remove:
            try:
                remove_track(ctx, run_, t, reason=f"duplicate of {g.keep.pid}")
                done += 1
            except Stop as e:
                ctx.say(f"  skipped {t.label()}: {e}")
    ctx.say(f"Removed {done} copies. Their files are kept in {run_.path}/removed")
    ctx.say(f"Undo with: trackmend undo {run_.id} --apply")
    return 0
