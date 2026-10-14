"""Last resort for artwork: the thumbnail of a YouTube video of the same song and length.

Only the search results are read (with yt-dlp, if installed); nothing is downloaded but
the thumbnail image. Such artwork is never written without being looked at first: it
goes to ``trackmend review``.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Dict, List, Optional

from .net import Http
from .text import core_title, norm, primary_artist

GAP = 2.0  # seconds


def search(query: str, limit: int = 8) -> List[Dict]:
    if not shutil.which("yt-dlp"):
        return []
    try:
        r = subprocess.run(["yt-dlp", "--flat-playlist", "--dump-single-json", "--no-warnings",
                            f"ytsearch{limit}:{query}"], capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return []
    if r.returncode != 0:
        return []
    try:
        entries = json.loads(r.stdout).get("entries") or []
    except ValueError:
        return []
    return [{"id": e.get("id"), "title": e.get("title") or "", "channel": e.get("channel") or e.get("uploader") or "",
             "duration": e.get("duration") or 0} for e in entries if e.get("id")]


def pick(entries: List[Dict], artist: str, title: str, duration: float) -> Optional[Dict]:
    """A video with the same length whose title names the song and the artist."""
    want_title = core_title(title)
    want_artist = norm(primary_artist(artist))
    for e in entries:
        if abs(float(e.get("duration") or 0) - duration) > GAP:
            continue
        hay = norm(f"{e.get('title')} {e.get('channel')}")
        if want_title and want_title in hay and (not want_artist or want_artist in hay):
            return e
    return None


def thumbnail(http: Http, covers_dir: str, artist: str, title: str, duration: float) -> Optional[bytes]:
    query = f"{artist} {title}".strip()
    entries = http.memo(f"yt-search:{query}", lambda: search(query))
    video = pick(entries if isinstance(entries, list) else [], artist, title, duration)
    if not video:
        return None
    for size in ("maxresdefault", "hqdefault"):
        data = http.download(f"https://i.ytimg.com/vi/{video['id']}/{size}.jpg", covers_dir)
        if data:
            return data
    return None
