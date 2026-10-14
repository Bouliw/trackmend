"""``trackmend reimport``: fix tracks whose duration in Music is wrong.

Music caches a track's duration when the file is added and ``refresh`` does not
recompute it. AAC files rewritten by other tools sometimes show 0:02 for a four-minute
song. The only fix is to take the track out of Music and add the same file again.
Play count and favorite are carried over; playlist membership is not.
"""
from __future__ import annotations

import os
from typing import List

from .ops import Context, Stop, fmt_duration, local_file, remove_track, shorten
from .scan import wrong_duration
from .music import Track
from .store import move


def run(ctx: Context, tracks: List[Track], args, argv: List[str]) -> int:
    targets = []
    for t in tracks:
        if local_file(t):
            real = wrong_duration(t)
            if real:
                targets.append((t, real))
    if not targets:
        ctx.say("Every track has the right duration in Music.")
        return 0
    ctx.say(f"{len(targets)} tracks show a wrong duration in Music:")
    for t, real in targets:
        ctx.say(f"  {shorten(t.label(), 60)}  Music {fmt_duration(t.duration)}, file {fmt_duration(real)}")
    if not args.apply:
        ctx.say("\nNothing was changed. Run again with --apply to add these files to Music again.")
        return 0
    if not ctx.confirm(f"Take {len(targets)} tracks out of Music and add their files again "
                       "(play count and favorite are kept, playlists are not)?"):
        return 0
    run_ = ctx.home.new_run("reimport", argv)
    fixed = 0
    for t, _ in targets:
        try:
            loc = ctx.music.location(t.pid)
            if not loc or not os.path.exists(loc):
                raise Stop("file not found")
            seq, kept = remove_track(ctx, run_, t, reason="reimport", op="reimport-remove")
            if kept and not os.path.exists(loc):
                move(kept, loc)
            (_, new_pid), = ctx.music.add([loc])
            if not new_pid:
                raise Stop(f"Music did not take the file back; it is at {loc}")
            ctx.music.set({new_pid: {"plays": t.plays, "favorited": t.favorited}})
            run_.done(seq, new_pid=new_pid)
            fixed += 1
        except Stop as e:
            ctx.say(f"  ! {t.label()}: {e}")
    ctx.say(f"{fixed} tracks added again.")
    return 0
