"""Artwork: finding it when a track has none, and fixing video frames used as artwork.

Order of the search for a track without artwork (after the Deezer match itself):

1. another track of the same album in the library that has artwork,
2. the album, on Deezer, then on the iTunes Search API (stores us, fr, jp), then on
   MusicBrainz with the Cover Art Archive,
3. the same song in another version (artwork only, never for live or cover versions),
4. last, the thumbnail of a YouTube video of the same length, cut to a square. That one
   is never written without being looked at (``trackmend review``).

Artwork taken from a YouTube video is a 16:9 or 4:3 frame, often with black bars.
The bars are found with ffmpeg's ``cropdetect`` and the image is cut to a centred square.
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import tempfile
import urllib.parse
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

from . import audio, text
from .deezer import Deezer
from . import youtube
from .music import Track
from .musicbrainz import MusicBrainz
from .net import Http, NetError

ITUNES = "https://itunes.apple.com/search"
STORES = ("us", "fr", "jp")
SQUARE_TOLERANCE = 0.03
NO_OTHER_VERSION = {"live", "acoustic", "cover"}


# --------------------------------------------------------------------------------------
# Image geometry, without an imaging library
# --------------------------------------------------------------------------------------

def image_size(data: Optional[bytes]) -> Optional[Tuple[int, int]]:
    """(width, height) of a JPEG or PNG."""
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + seg_len
    return None


VIDEO_RATIO = 1.25  # 4:3 is 1.33 and 16:9 is 1.78; scanned sleeves are often 1.05 to 1.15


def is_video_frame(data: Optional[bytes]) -> bool:
    """A 4:3 or 16:9 picture, as taken from a video. Slightly uneven scans of real sleeves are not."""
    size = image_size(data)
    if not size or not min(size):
        return False
    return max(size) / min(size) >= VIDEO_RATIO


def is_square(data: Optional[bytes]) -> bool:
    size = image_size(data)
    if not size or not size[1]:
        return False
    w, h = size
    return abs(w - h) <= SQUARE_TOLERANCE * max(w, h)


def _ffmpeg(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-y", *args], capture_output=True, text=True)


def squarify(data: bytes) -> Optional[bytes]:
    """Remove black bars, then keep the centred square. Needs ffmpeg; returns JPEG bytes."""
    if not shutil.which("ffmpeg") or not image_size(data):
        return None
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.png" if data[:4] == b"\x89PNG" else "in.jpg")
        dst = os.path.join(d, "out.jpg")
        with open(src, "wb") as fh:
            fh.write(data)
        probe = _ffmpeg("-loop", "1", "-t", "0.2", "-i", src, "-vf", "cropdetect=limit=0.1:round=2:reset=0",
                        "-f", "null", "-")
        boxes = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", probe.stderr)
        filters = []
        if boxes:
            w, h, x, y = (int(v) for v in boxes[-1])
            iw, ih = image_size(data)  # type: ignore[misc]
            if w > 16 and h > 16 and (w, h) != (iw, ih):
                filters.append(f"crop={w}:{h}:{x}:{y}")
        filters.append("crop='min(iw,ih)':'min(iw,ih)'")
        r = _ffmpeg("-i", src, "-vf", ",".join(filters), "-q:v", "2", "-frames:v", "1", dst)
        if r.returncode != 0 or not os.path.exists(dst):
            return None
        with open(dst, "rb") as fh:
            return fh.read()


# --------------------------------------------------------------------------------------
# iTunes Search API
# --------------------------------------------------------------------------------------

def _big(url: str) -> str:
    return re.sub(r"/\d+x\d+bb\.(jpg|png)$", r"/1000x1000bb.\1", url or "")


class ITunes:
    """Artwork from the public iTunes Search API (no key; about 20 requests per minute)."""

    def __init__(self, http: Http, stores: Sequence[str] = STORES):
        self.http, self.stores = http, stores

    def _search(self, term: str, entity: str, store: str) -> List[dict]:
        q = urllib.parse.urlencode({"term": term[:120], "entity": entity, "country": store, "limit": 10})
        d = self.http.get_json(f"{ITUNES}?{q}")
        return list((d or {}).get("results") or []) if isinstance(d, dict) else []

    def album_cover(self, artist: str, album: str) -> str:
        if not album:
            return ""
        for store in self.stores:
            for r in self._search(f"{artist} {album}", "album", store):
                if text.same_artist(r.get("artistName", ""), artist) and (
                        text.norm(r.get("collectionName")) == text.norm(album)
                        or text.similarity(r.get("collectionName", ""), album) >= 0.9):
                    return _big(r.get("artworkUrl100", ""))
        return ""

    def song_cover(self, artist: str, title: str) -> str:
        for store in self.stores:
            for r in self._search(f"{artist} {title}", "song", store):
                if text.same_artist(r.get("artistName", ""), artist) and \
                        text.core_title(r.get("trackName", "")) == text.core_title(title):
                    return _big(r.get("artworkUrl100", ""))
        return ""


# --------------------------------------------------------------------------------------
# The search chain
# --------------------------------------------------------------------------------------

def album_key(album: str, artist: str) -> Tuple[str, str]:
    return text.norm(album), text.compact(text.primary_artist(artist))


class ArtworkFinder:
    def __init__(self, http: Http, covers_dir: str, library: List[Track], itunes: bool = True,
                 musicbrainz: bool = True, youtube_thumbnails: bool = True):
        self.http, self.covers_dir = http, covers_dir
        self.deezer = Deezer(http)
        self.itunes = ITunes(http) if itunes else None
        self.mb = MusicBrainz(http) if musicbrainz else None
        self.youtube = youtube_thumbnails
        self.albums: Dict[Tuple[str, str], List[Track]] = defaultdict(list)
        for t in library:
            if t.album.strip() and t.location:
                self.albums[album_key(t.album, t.album_artist or t.artist)].append(t)

    def _download(self, url: str) -> Optional[bytes]:
        if not url:
            return None
        data = self.http.download(url, self.covers_dir)
        return data if data and image_size(data) else None

    def from_library(self, t: Track) -> Optional[bytes]:
        for other in self.albums.get(album_key(t.album, t.album_artist or t.artist), []):
            if other.pid == t.pid or not other.location or not os.path.exists(other.location):
                continue
            try:
                data = audio.cover_bytes(other.location)
            except audio.AudioError:
                continue
            if data and not is_video_frame(data):
                return data
        return None

    def find(self, t: Track, artist: str, title: str, album: str,
             qualifiers: Sequence[str] = ()) -> Tuple[Optional[bytes], str, bool]:
        """(artwork, where it came from, needs a look before it is written)."""
        if album:
            data = self.from_library(Track(pid=t.pid, album=album, artist=artist, album_artist=t.album_artist))
            if data:
                return data, "same album in the library", False
        try:
            if artist and album:
                data = self._download(self.deezer.album_cover(artist, album))
                if data:
                    return data, "album on Deezer", False
                if self.itunes:
                    data = self._download(self.itunes.album_cover(artist, album))
                    if data:
                        return data, "album on iTunes", False
                if self.mb:
                    for url in self.mb.cover_urls(artist, album)[:3]:
                        data = self._download(url)
                        if data:
                            return data, "album on MusicBrainz (Cover Art Archive)", False
            if artist and not (set(qualifiers) & NO_OTHER_VERSION):
                data = self._download(self.deezer.any_version_cover(artist, title))
                if data:
                    return data, "same song, other version, on Deezer", False
                if self.itunes:
                    data = self._download(self.itunes.song_cover(artist, title))
                    if data:
                        return data, "same song, other version, on iTunes", False
            if self.youtube and artist:
                data = youtube.thumbnail(self.http, self.covers_dir, artist, title, t.duration)
                if data:
                    return data, "YouTube thumbnail of a video of the same length", True
        except NetError:
            pass
        return None, "", False
