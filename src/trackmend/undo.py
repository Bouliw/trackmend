"""``trackmend undo``: put back what a run changed, from its journal and backups."""
from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

from . import audio
from .music import Track
from .ops import Context, Stop, Syncer, add_files, music_values, remove_track, shorten
from .store import Run, move

UNDOABLE = {"tags", "lyrics", "remove", "add"}
NUMERIC = {"year", "track", "track_total", "disc"}


def pending(run: Run) -> List[Dict]:
    return [e for e in run.entries() if e.get("op") in UNDOABLE and e.get("status") in ("pending", "done")]


def list_runs(ctx: Context) -> int:
    runs = ctx.home.runs()
    if not runs:
        ctx.say("No runs yet.")
        return 0
    ctx.say("Runs (most recent last):")
    for r in runs:
        meta = r.meta()
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(meta.get("started", 0)))
        left = len(pending(r))
        total = sum(1 for e in r.entries() if e.get("op") in UNDOABLE)
        state = f"{left} changes can be undone" if left else ("undone" if total else "nothing changed")
        ctx.say(f"  {r.id:<32} {when}  {state}")
    return 0


def describe(e: Dict) -> str:
    op, label = e["op"], shorten(e.get("label") or e.get("path") or e.get("pid", ""), 60)
    if op == "tags":
        return f"restore tags      {label}"
    if op == "lyrics":
        return f"remove lyrics     {label}"
    if op == "remove":
        return f"add back          {label}"
    return f"take out again    {os.path.basename(e.get('path') or '')}"


def _cleared(before: Dict) -> Dict:
    """Fields that were empty before the run: refresh will not empty them, so Music must."""
    out = {}
    for k, v in (before or {}).items():
        if v in (None, "", 0, "0"):
            out[k] = 0 if k in NUMERIC else ""
    return out


def run(ctx: Context, args, argv: List[str]) -> int:
    if args.list:
        return list_runs(ctx)
    target: Optional[Run]
    if args.run:
        target = ctx.home.run(args.run)
        if not target:
            raise Stop(f"no run named {args.run} (see: trackmend undo --list)")
    else:
        target = next((r for r in reversed(ctx.home.runs()) if pending(r)), None)
        if not target:
            ctx.say("Nothing to undo.")
            return 0
    todo = list(reversed(pending(target)))
    if not todo:
        ctx.say(f"{target.id}: nothing left to undo.")
        return 0
    ctx.say(f"{target.id}: {len(todo)} changes to undo")
    for e in todo[:200]:
        ctx.say(f"  {describe(e)}")
    if len(todo) > 200:
        ctx.say(f"  ... and {len(todo) - 200} more")
    if not args.apply:
        ctx.say("\nNothing was changed. Run again with --apply to undo.")
        return 0
    removals = sum(1 for e in todo if e["op"] == "add")
    if removals and not ctx.confirm(f"This takes {removals} tracks added by the run out of Music again."):
        return 0

    undo_run = ctx.home.new_run("undo", argv)
    syncer = Syncer(ctx, None)
    forget: Dict[str, List[str]] = {}
    done = skipped = 0
    for e in todo:
        seq, pid, op = int(e["seq"]), e.get("pid", ""), e["op"]
        try:
            if op == "tags":
                if not e.get("backup"):
                    target.mark_undone(seq)  # stopped before the file was written
                    continue
                loc = ctx.music.location(pid)
                if not loc or not os.path.exists(loc):
                    raise Stop("track or file no longer in Music")
                if e.get("via") == "music":
                    before = e.get("before") or {}
                    if before:
                        errors = ctx.music.set({pid: music_values(before)})
                        if errors:
                            raise Stop(errors[0])
                    if e.get("cover"):
                        old = None
                        if e.get("old_cover") and os.path.exists(e["old_cover"]):
                            with open(e["old_cover"], "rb") as fh:
                                old = fh.read()
                        ctx.music.set_artwork(pid, old)
                else:
                    audio.restore(loc, e["backup"])
                    syncer.add(pid, _cleared(e.get("before") or {}), artwork="match" if e.get("cover") else "")
                forget.setdefault("tags", []).append(pid)
            elif op == "lyrics":
                errors = ctx.music.set({pid: {"lyrics": e.get("before") or ""}})
                if errors:
                    raise Stop(errors[0])
                forget.setdefault("lyrics", []).append(pid)
            elif op == "remove":
                kept, orig = e.get("kept"), e.get("path")
                if not kept or not os.path.exists(kept):
                    raise Stop("its file is no longer in the run folder")
                dest = orig if orig and not os.path.exists(orig) else kept
                if dest != kept:
                    move(kept, dest)
                (_, new_pid), = add_files(ctx, undo_run, [dest], op="readd")
                if not new_pid:
                    raise Stop("Music did not accept the file")
                old = e.get("track") or {}
                ctx.music.set({new_pid: {"plays": int(old.get("plays") or 0),
                                         "favorited": bool(old.get("favorited"))}})
            elif op == "add":
                t = Track(pid=pid, name=os.path.basename(e.get("path") or ""))
                if ctx.music.location(pid) is None and not ctx.music.info([pid]).get(pid):
                    raise Stop("already gone from Music")
                remove_track(ctx, undo_run, t, reason=f"undo {target.id}")
            target.mark_undone(seq)
            done += 1
        except (Stop, audio.AudioError, OSError) as err:
            skipped += 1
            ctx.say(f"  ! {describe(e)}: {err}")
    syncer.flush()
    for command, pids in forget.items():
        p = ctx.home.progress(command)
        p.forget(pids)
        p.close()
    ctx.say(f"Undone: {done}, skipped: {skipped}.")
    return 0 if not skipped else 1
