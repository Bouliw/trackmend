"""Stand-ins for the Music app and the network, so tests never touch a real library."""
from __future__ import annotations

import json
import os
import shutil
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from trackmend import audio, text
from trackmend.music import PROPS, Track, to_jpeg

_ATTR = {"title": "name", "artist": "artist", "album": "album", "album_artist": "album_artist",
         "year": "year", "genre": "genre", "track": "track_number", "plays": "plays",
         "favorited": "favorited"}
_FROM_PROP = {v: k for k, v in PROPS.items()}


class FakeMusic:
    """Behaves like Music where it matters for trackmend:

    * ``refresh`` re-reads tags but never empties a field and never fixes the duration;
    * ``refresh`` never changes the artwork Music shows (see ``shown_artwork``): only
      ``set_artwork`` does, as in Music, which keeps its cached image;
    * with ``organize=True``, ``refresh`` renames the file the way Music does;
    * ``delete`` sends files inside the media folder to the Trash;
    * with ``follow_moves=True``, a moved file is still found (like an alias).
    """

    def __init__(self, media: str, organize: bool = False, follow_moves: bool = False):
        self.media = media
        self.trash = os.path.join(os.path.dirname(media.rstrip("/")), "Trash")
        self.organize, self.follow = organize, follow_moves
        self._tracks: Dict[str, Track] = {}
        self._lyrics: Dict[str, str] = {}
        self._inode: Dict[str, Tuple[int, int]] = {}
        self._shown: Dict[str, Optional[bytes]] = {}
        self.playlists: Dict[str, List[str]] = {}
        self.calls: List[Tuple[str, object]] = []
        self._n = 0
        os.makedirs(media, exist_ok=True)

    # -- test helpers ------------------------------------------------------------------
    def add_file(self, path: str, playlist: Optional[str] = None, **fields) -> str:
        self._n += 1
        pid = f"{self._n:016X}"
        tags = audio.read(path)
        t = Track(pid=pid, name=tags.title or os.path.splitext(os.path.basename(path))[0],
                  artist=tags.artist, album=tags.album, album_artist=tags.album_artist,
                  year=int(tags.year or 0), genre=tags.genre, duration=tags.length, location=path,
                  bit_rate=128, track_number=tags.track, date_added=f"2020-01-{self._n % 28 + 1:02d}",
                  kind="MPEG audio file")
        for k, v in fields.items():
            setattr(t, k, v)
        self._tracks[pid] = t
        self._shown[pid] = audio.cover_bytes(path)
        st = os.stat(path)
        self._inode[pid] = (st.st_dev, st.st_ino)
        if playlist:
            self.playlists.setdefault(playlist, []).append(pid)
        return pid

    def get(self, pid: str) -> Track:
        return self._tracks[pid]

    def shown_artwork(self, pid: str) -> Optional[bytes]:
        """The image Music displays for the track (its cache, not the file)."""
        return self._shown.get(pid)

    def all(self) -> List[Track]:
        return list(self._tracks.values())

    def _find_moved(self, pid: str) -> Optional[str]:
        want = self._inode.get(pid)
        root = os.path.dirname(self.media.rstrip("/"))
        for d, _, files in os.walk(root):
            for f in files:
                p = os.path.join(d, f)
                st = os.stat(p)
                if (st.st_dev, st.st_ino) == want:
                    return p
        return None

    # -- Library protocol --------------------------------------------------------------
    def tracks(self, playlist: Optional[str] = None) -> List[Track]:
        self.calls.append(("tracks", playlist))
        if playlist is not None and playlist not in self.playlists:
            from trackmend.music import MusicError
            raise MusicError(f"playlist not found: {playlist}")
        pids = self.playlists[playlist] if playlist else list(self._tracks)
        out = []
        for pid in pids:
            t = Track(**self._tracks[pid].to_dict())
            t.lyrics_len = len(self._lyrics.get(pid, ""))
            out.append(t)
        return out

    def info(self, pids: Sequence[str]) -> Dict[str, Optional[Dict]]:
        res: Dict[str, Optional[Dict]] = {}
        for pid in pids:
            t = self._tracks.get(pid)
            res[pid] = None if t is None else {"pid": pid, "location": self.location(pid), "duration": t.duration,
                                               "name": t.name, "artist": t.artist, "album": t.album,
                                               "lyrics": self._lyrics.get(pid, ""), "plays": t.plays}
        return res

    def location(self, pid: str) -> Optional[str]:
        self.calls.append(("location", pid))
        t = self._tracks.get(pid)
        if t is None:
            return None
        if self.follow and t.location and not self._same_file(pid, t.location):
            found = self._find_moved(pid)
            if found:
                t.location = found
        return t.location

    def _same_file(self, pid: str, path: str) -> bool:
        if not os.path.exists(path):
            return False
        st = os.stat(path)
        return (st.st_dev, st.st_ino) == self._inode.get(pid)

    def lyrics(self, pid: str) -> str:
        return self._lyrics.get(pid, "")

    def refresh(self, pids: Sequence[str]) -> List[str]:
        self.calls.append(("refresh", list(pids)))
        failed = []
        for pid in pids:
            t = self._tracks.get(pid)
            if t is None or not t.location or not os.path.exists(t.location):
                failed.append(pid)
                continue
            tags = audio.read(t.location)
            for k, attr in _ATTR.items():
                v = getattr(tags, k, None)
                if v not in (None, "", 0):
                    setattr(t, attr, int(v) if attr == "year" else v)
            if self.organize and t.location.startswith(self.media):
                dst = os.path.join(self.media, text.safe_filename(t.artist or "Unknown Artist"),
                                   text.safe_filename(t.album or "Unknown Album"),
                                   text.safe_filename(t.name) + audio.ext(t.location))
                if dst != t.location:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.move(t.location, dst)
                    t.location = dst
        return failed

    def set(self, changes: Dict[str, Dict]) -> List[str]:
        self.calls.append(("set", json.loads(json.dumps(changes))))
        errors = []
        for pid, props in changes.items():
            t = self._tracks.get(pid)
            if t is None:
                errors.append(f"{pid}: not found")
                continue
            for k, v in props.items():
                k = _FROM_PROP.get(k, k)
                if k == "lyrics":
                    self._lyrics[pid] = v
                elif k in _ATTR:
                    setattr(t, _ATTR[k], v)
            # Music writes the fields it changed into the file
            file_fields = {_FROM_PROP.get(k, k): v for k, v in props.items()}
            file_fields = {k: v for k, v in file_fields.items() if k in audio.FIELDS}
            if file_fields and t.location and os.path.exists(t.location):
                audio.write(t.location, file_fields)
        return errors

    def set_artwork(self, pid: str, image: Optional[bytes]) -> None:
        """Like MusicApp: PNG becomes JPEG, Music shows it and writes it into the file."""
        self.calls.append(("set_artwork", pid))
        t = self._tracks[pid]
        if image is not None:
            image = to_jpeg(image)
        self._shown[pid] = image
        if image is None:
            f = audio._open(t.location)
            if f.tags is not None:
                for key in [k for k in f.tags.keys() if k == "covr" or k.startswith("APIC")]:
                    del f.tags[key]
                f.save()
        else:
            audio.write(t.location, {}, image)

    def add(self, paths: Sequence[str]) -> List[Tuple[str, Optional[str]]]:
        self.calls.append(("add", list(paths)))
        out = []
        for p in paths:
            same = [pid for pid, t in self._tracks.items() if t.location == p]
            out.append((p, same[0] if same else (self.add_file(p) if os.path.exists(p) else None)))
        return out

    def delete(self, pids: Sequence[str]) -> List[str]:
        self.calls.append(("delete", list(pids)))
        errors = []
        for pid in pids:
            t = self._tracks.pop(pid, None)
            if t is None:
                errors.append(f"{pid}: not found")
                continue
            loc = t.location
            if self.follow and loc and not self._same_file(pid, loc):
                loc = self._find_moved(pid) or loc
            if loc and os.path.exists(loc) and os.path.abspath(loc).startswith(os.path.abspath(self.media)):
                os.makedirs(self.trash, exist_ok=True)
                shutil.move(loc, os.path.join(self.trash, os.path.basename(loc)))
            for pl in self.playlists.values():
                if pid in pl:
                    pl.remove(pid)
        return errors


Response = Union[dict, list, Tuple[int, bytes], Callable[[str], object]]


class FakeHttp:
    """Transport for :class:`trackmend.net.Http`: answers from a table of URL fragments."""

    def __init__(self, routes: Optional[Dict[str, Response]] = None, default: Response = (404, b"")):
        self.routes = dict(routes or {})
        self.default = default
        self.calls: List[str] = []

    def __call__(self, url: str, headers: Dict[str, str]) -> Tuple[int, bytes]:
        self.calls.append(url)
        hits = [k for k in self.routes if k in url]
        r = self.routes[max(hits, key=len)] if hits else self.default
        if callable(r):
            r = r(url)
        if isinstance(r, tuple):
            return r
        return 200, json.dumps(r).encode()

    def count(self, fragment: str) -> int:
        return sum(1 for u in self.calls if fragment in u)
