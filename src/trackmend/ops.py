"""Building blocks shared by the commands: context, safe writes, removals, confirmations."""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol, Sequence, TextIO, Tuple

from . import audio
from .music import Track
from .net import Http
from .store import Home, Progress, Run, move

MIN_SONG = 30       # seconds; shorter files are jingles, sound effects or voice notes
LONG_FILE = 20 * 60 # seconds; longer files are albums or mixes: candidates for "split"


class Library(Protocol):
    """What the commands need from Music. :class:`trackmend.music.MusicApp` is the real one."""

    def tracks(self, playlist: Optional[str] = None) -> List[Track]: ...
    def info(self, pids: Sequence[str]) -> Dict[str, Optional[Dict]]: ...
    def location(self, pid: str) -> Optional[str]: ...
    def lyrics(self, pid: str) -> str: ...
    def refresh(self, pids: Sequence[str]) -> List[str]: ...
    def set(self, changes: Dict[str, Dict]) -> List[str]: ...
    def add(self, paths: Sequence[str]) -> List[Tuple[str, Optional[str]]]: ...
    def delete(self, pids: Sequence[str]) -> List[str]: ...
    def set_artwork(self, pid: str, image: Optional[bytes]) -> None: ...


class Stop(Exception):
    """A command refused to go on; the message says why."""


@dataclass
class Context:
    music: Library
    home: Home
    out: TextIO = field(default_factory=lambda: sys.stdout)
    assume_yes: bool = False
    interactive: Optional[bool] = None
    http_factory: Optional[Callable[[], Http]] = None
    _http: Optional[Http] = None

    def say(self, *parts: object) -> None:
        print(*parts, file=self.out, flush=True)

    def http(self) -> Http:
        if self._http is None:
            self._http = self.http_factory() if self.http_factory else Http(self.home.cache_db, log=self.say)
        return self._http

    def confirm(self, question: str) -> bool:
        """Ask before removing anything. Without a terminal, only ``--yes`` says yes."""
        if self.assume_yes:
            return True
        interactive = sys.stdin.isatty() if self.interactive is None else self.interactive
        if not interactive:
            self.say(f"{question} -> not confirmed (no terminal; add --yes to confirm).")
            return False
        try:
            answer = input(f"{question} Type 'yes' to continue: ")
        except EOFError:
            return False
        return answer.strip().lower() in ("yes", "y")


def local_file(t: Track) -> bool:
    return bool(t.location) and audio.is_supported(t.location)


def fresh_location(ctx: Context, pid: str) -> str:
    """Where the file is right now (Music may have moved it since the export)."""
    loc = ctx.music.location(pid)
    if not loc or not os.path.exists(loc):
        raise Stop("file not found")
    return loc


class Syncer:
    """Makes Music take what was written into the files, in small batches.

    * ``refresh`` makes Music read the tags again, but never empties a field that was
      removed from the file: such fields are cleared in Music directly afterwards.
    * Artwork is never left to ``refresh``: Music keeps showing the image it had cached
      (or the grey note) even though the file and AppleScript both have the new one.
      After the refresh, the file's artwork is handed to Music itself.
    """

    def __init__(self, ctx: Context, progress: Optional[Progress], every: int = 20):
        self.ctx, self.progress, self.every = ctx, progress, every
        self.pending: Dict[str, Dict] = {}
        self.artwork: Dict[str, str] = {}

    def add(self, pid: str, cleared: Optional[Dict] = None, artwork: str = "") -> None:
        """``artwork="show"`` hands the file's artwork to Music; ``"match"`` also removes
        Music's artwork when the file has none (used when undoing)."""
        self.pending[pid] = cleared or {}
        if artwork and self.artwork.get(pid) != "match":
            self.artwork[pid] = artwork
        if len(self.pending) >= self.every:
            self.flush()

    def flush(self) -> List[str]:
        if not self.pending:
            return []
        pids = list(self.pending)
        failed = set(self.ctx.music.refresh(pids))
        clears = {pid: c for pid, c in self.pending.items() if c and pid not in failed}
        if clears:
            self.ctx.music.set(clears)
        for pid in pids:
            mode = self.artwork.get(pid)
            if pid in failed or not mode:
                continue
            try:
                loc = self.ctx.music.location(pid)
                image = audio.cover_bytes(loc) if loc and os.path.exists(loc) else None
                if image or mode == "match":
                    self.ctx.music.set_artwork(pid, image)
            except Exception as e:  # one bad image must not stop the batch
                self.ctx.say(f"  ! Music did not take the artwork of {pid}: {e}")
        if self.progress:
            self.progress.mark_synced([p for p in pids if p not in failed])
        self.pending.clear()
        self.artwork.clear()
        if failed:
            self.ctx.say(f"  Music could not re-read {len(failed)} file(s); run the command again to retry.")
        return sorted(failed)


NUMERIC = {"year", "track", "track_total", "disc"}


def music_values(changes: Dict[str, object]) -> Dict[str, object]:
    """Field values as Music's scripting dictionary wants them (numbers, "" or 0 to empty)."""
    out: Dict[str, object] = {}
    for k, v in changes.items():
        if k in NUMERIC:
            try:
                out[k] = int(str(v)[:4] if k == "year" else v) if v not in (None, "") else 0
            except ValueError:
                out[k] = 0
        else:
            out[k] = "" if v is None else str(v)
    return out


def write_tags(ctx: Context, run: Run, track: Track, changes: Dict[str, object],
               cover: Optional[bytes], op: str = "tags", before: Optional[Dict] = None,
               via: str = "files") -> int:
    """Back up then write one track. The journal is written before and after.

    ``via="files"`` writes the file (then Music must re-read it); ``via="music"`` asks
    Music to change the fields and the artwork itself, which is safer while other
    scripts are working on the library.
    """
    loc = fresh_location(ctx, track.pid)
    seq = run.begin(op, pid=track.pid, path=loc, label=track.label(), changes=changes,
                    cover=bool(cover), before=before or {}, via=via)
    base = run.backup_base(seq)
    old_cover = audio.cover_bytes(loc)
    if old_cover:
        with open(base + ".cover", "wb") as fh:
            fh.write(old_cover)
    run.note(seq, backup=audio.snapshot(loc, base), old_cover=(base + ".cover") if old_cover else None)
    if via == "music":
        if changes:
            errors = ctx.music.set({track.pid: music_values(changes)})
            if errors:
                raise Stop(f"Music refused the change: {errors[0]}")
        if cover:
            ctx.music.set_artwork(track.pid, cover)
    else:
        audio.write(loc, changes, cover)
    run.done(seq)
    return seq


def remove_track(ctx: Context, run: Run, track: Track, reason: str, op: str = "remove") -> Tuple[int, Optional[str]]:
    """Take a track out of Music without losing its file.

    The file is moved into the run's ``removed`` folder first, so Music has nothing to
    send to the Trash. If Music follows the moved file (it can on the same volume),
    the file is put back, copied instead, and the original goes where Music sends it.
    """
    loc = ctx.music.location(track.pid)
    seq = run.begin(op, pid=track.pid, path=loc, label=track.label(), reason=reason,
                    track=track.to_dict())
    kept = None
    if loc and os.path.exists(loc):
        kept = run.removed_path(seq, loc)
        move(loc, kept)
        now = ctx.music.location(track.pid)
        if now and os.path.exists(now) and os.path.samefile(now, kept):
            move(kept, loc)
            shutil.copy2(loc, kept)
        run.note(seq, kept=kept)
    errors = ctx.music.delete([track.pid])
    if errors:
        if kept and not os.path.exists(loc or ""):
            move(kept, loc)
        raise Stop(f"Music refused to remove {track.label()}: {errors[0]}")
    if loc and os.path.exists(loc) and kept:
        os.remove(loc)  # outside the Music folder: Music leaves it, the copy is kept
    run.done(seq)
    return seq, kept


def add_files(ctx: Context, run: Run, paths: Sequence[str], op: str = "add") -> List[Tuple[str, Optional[str]]]:
    """Add files to Music. Music may not read the Desktop, Documents or Downloads
    folders when a script asks, so files there are moved to ~/Music/trackmend first."""
    from .music import readable_by_music
    safe = []
    for p in paths:
        if not readable_by_music(p):
            dst = os.path.join(os.path.expanduser("~/Music/trackmend/Imported"), os.path.basename(p))
            move(p, dst)
            ctx.say(f"  moved {os.path.basename(p)} to {os.path.dirname(dst)} (Music cannot read that folder)")
            p = dst
        safe.append(p)
    paths = safe
    added = ctx.music.add(list(paths))
    for path, pid in added:
        if pid:
            seq = run.begin(op, pid=pid, path=path)
            run.done(seq, location=ctx.music.location(pid))
    return added


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds or 0))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def shorten(s: str, width: int) -> str:
    s = s or ""
    return s if len(s) <= width else s[: width - 1] + "…"
