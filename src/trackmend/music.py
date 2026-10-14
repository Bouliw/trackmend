"""Talking to the Music app (osascript, AppleScript and JavaScript for Automation).

Tracks are always addressed by their persistent ID. File paths are never kept between
two operations: when "Keep Music Media folder organized" is on, Music renames and
moves a file right after it reads its tags again, so the location is read just
before each write.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from importlib import resources
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


class MusicError(Exception):
    pass


@dataclass
class Track:
    pid: str
    name: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    year: int = 0
    genre: str = ""
    duration: float = 0.0
    location: Optional[str] = None
    plays: int = 0
    bit_rate: int = 0
    lyrics_len: int = 0
    track_number: int = 0
    date_added: str = ""
    kind: str = ""
    favorited: bool = False

    @classmethod
    def from_dict(cls, d: Dict) -> "Track":
        known = {k: d.get(k) for k in cls.__dataclass_fields__ if k in d}
        t = cls(**known)
        t.year = int(t.year or 0)
        t.duration = float(t.duration or 0)
        t.plays = int(t.plays or 0)
        t.bit_rate = int(t.bit_rate or 0)
        t.lyrics_len = int(t.lyrics_len or 0)
        return t

    def label(self) -> str:
        return f"{self.artist} - {self.name}" if self.artist else self.name

    def to_dict(self) -> Dict:
        return asdict(self)


# Property names used by Music's scripting dictionary.
PROPS = {"title": "name", "artist": "artist", "album": "album", "album_artist": "albumArtist",
         "year": "year", "genre": "genre", "track": "trackNumber", "track_total": "trackCount",
         "disc": "discNumber", "lyrics": "lyrics", "plays": "playedCount", "favorited": "favorited"}


def _script(name: str) -> str:
    return str(resources.files("trackmend").joinpath("osa", name))


class MusicApp:
    """The real Music app. Every method runs one osascript process."""

    batch = 40

    def __init__(self, timeout: float = 600):
        if sys.platform != "darwin":
            raise MusicError("this command needs the Music app, so it only runs on macOS")
        self.timeout = timeout

    # -- plumbing ----------------------------------------------------------------------
    def _osa(self, args: Sequence[str], js: bool = False) -> str:
        cmd = ["osascript"] + (["-l", "JavaScript"] if js else []) + list(args)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            raise MusicError("Music did not answer in time") from e
        if r.returncode != 0:
            msg = r.stderr.strip()
            if "-1743" in msg or "Not authorized" in msg:
                msg += ("\nAllow your terminal to control Music in System Settings > "
                        "Privacy & Security > Automation.")
            raise MusicError(msg or "osascript failed")
        return r.stdout.strip()

    def _js_json(self, cmd: str, payload) -> object:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            path = fh.name
        try:
            return json.loads(self._osa([_script("track.js"), cmd, path], js=True) or "null")
        finally:
            os.unlink(path)

    # -- reading -----------------------------------------------------------------------
    def tracks(self, playlist: Optional[str] = None) -> List[Track]:
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "tracks.json")
            self._osa([_script("export.js"), out] + ([playlist] if playlist else []), js=True)
            with open(out, encoding="utf-8") as fh:
                return [Track.from_dict(x) for x in json.load(fh)]

    def info(self, pids: Sequence[str]) -> Dict[str, Optional[Dict]]:
        res: Dict[str, Optional[Dict]] = {}
        for i in range(0, len(pids), self.batch):
            chunk = list(pids[i:i + self.batch])
            for pid, d in zip(chunk, self._js_json("info", chunk) or []):
                res[pid] = d
        return res

    def location(self, pid: str) -> Optional[str]:
        d = self.info([pid]).get(pid)
        return d.get("location") if d else None

    def lyrics(self, pid: str) -> str:
        d = self.info([pid]).get(pid)
        return (d or {}).get("lyrics") or ""

    # -- writing -----------------------------------------------------------------------
    def refresh(self, pids: Sequence[str]) -> List[str]:
        """Re-read tags from the files. Returns the IDs that failed."""
        failed: List[str] = []
        for i in range(0, len(pids), self.batch):
            out = self._osa([_script("refresh.applescript")] + list(pids[i:i + self.batch]))
            _, _, bad = out.partition("|")
            failed += [x for x in bad.split(",") if x]
        return failed

    def set(self, changes: Dict[str, Dict]) -> List[str]:
        """Set properties directly in Music. ``changes`` = {pid: {field: value}}."""
        errors: List[str] = []
        items = list(changes.items())
        for i in range(0, len(items), self.batch):
            payload = {pid: {PROPS.get(k, k): v for k, v in props.items()}
                       for pid, props in items[i:i + self.batch]}
            r = self._js_json("set", payload) or {}
            errors += r.get("errors", [])
        return errors

    def add(self, paths: Sequence[str]) -> List[Tuple[str, Optional[str]]]:
        """Add files. Returns (path, persistent ID or None) pairs."""
        out: List[Tuple[str, Optional[str]]] = []
        for i in range(0, len(paths), self.batch):
            chunk = list(paths[i:i + self.batch])
            lines = self._osa([_script("add.applescript")] + chunk).splitlines()
            for p, line in zip(chunk, lines + [""] * (len(chunk) - len(lines))):
                line = line.strip()
                out.append((p, None if not line or line.startswith("ERROR") else line))
        return out

    def delete(self, pids: Iterable[str]) -> List[str]:
        r = self._js_json("delete", list(pids)) or {}
        return r.get("errors", [])

    def set_artwork(self, pid: str, image: Optional[bytes]) -> None:
        """Give the artwork to Music itself (``None`` removes it); Music also writes it into the file.

        Writing the image into the file and asking for ``refresh`` is not enough: Music
        keeps showing what it had cached. PNG images are turned into JPEG first, as the
        AppleScript reads them ``as JPEG picture``.
        """
        if image is None:
            self._osa([_script("artwork.applescript"), "delete", pid])
            return
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
            fh.write(to_jpeg(image))
            path = fh.name
        try:
            self._osa([_script("artwork.applescript"), "set", pid, path])
        finally:
            os.unlink(path)


def to_jpeg(image: bytes) -> bytes:
    """JPEG bytes for any JPEG or PNG image (with macOS sips, or ffmpeg)."""
    if image[:3] == b"\xff\xd8\xff":
        return image
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "in.png"), os.path.join(d, "out.jpg")
        with open(src, "wb") as fh:
            fh.write(image)
        if shutil.which("sips"):
            cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "95", src, "--out", dst]
        elif shutil.which("ffmpeg"):
            cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", src, "-q:v", "2", dst]
        else:
            raise MusicError("cannot turn a PNG image into JPEG (no sips or ffmpeg)")
        subprocess.run(cmd, capture_output=True, check=False)
        if not os.path.exists(dst):
            raise MusicError("could not turn a PNG image into JPEG")
        with open(dst, "rb") as fh:
            return fh.read()


# Folders the Music app is not allowed to read when asked by a script (macOS privacy).
PROTECTED = ("~/Desktop", "~/Documents", "~/Downloads", "~/Library/Mobile Documents")


def readable_by_music(path: str) -> bool:
    p = os.path.realpath(path)
    return not any(p == os.path.realpath(os.path.expanduser(d)) or
                   p.startswith(os.path.realpath(os.path.expanduser(d)) + os.sep) for d in PROTECTED)


def other_scripts_running() -> List[str]:
    """Other processes driving apps with AppleScript right now (osascript)."""
    try:
        r = subprocess.run(["pgrep", "-fl", "osascript"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    mine = str(os.getpid())
    return [line for line in r.stdout.splitlines() if line.strip() and not line.startswith(mine)]
