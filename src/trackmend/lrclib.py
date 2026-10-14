"""Finding lyrics on LRCLIB (https://lrclib.net), a free and open lyrics database."""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional

from .net import Http
from .text import (BRACKET, Parsed, compact, core_title, different_script, primary_artist, same_artist, similarity,
                   split_featuring)

API = "https://lrclib.net/api/"

SAME_SONG_GAP = 120  # seconds, when artist and title are exactly the same
STRICT_GAP = 4       # seconds, otherwise

_TIMESTAMP = re.compile(r"^\s*(?:\[\d+:\d+(?:[.:]\d+)?\])+\s?", re.M)


@dataclass
class Lyrics:
    text: str
    lrclib_id: int
    artist: str
    title: str
    gap: float
    exact: bool
    instrumental: bool = False


def to_text(hit: dict) -> str:
    if hit.get("plainLyrics"):
        return hit["plainLyrics"].strip()
    if hit.get("syncedLyrics"):
        return _TIMESTAMP.sub("", hit["syncedLyrics"]).strip()
    return ""


def exact_same(hit: dict, artist: str, title: str) -> bool:
    """Same main artist and same title once both are normalized."""
    if not artist:
        return False
    a1, a2 = compact(primary_artist(hit.get("artistName", ""))), compact(primary_artist(artist))
    return bool(a1) and a1 == a2 and core_title(hit.get("trackName", "")) == core_title(title)


def title_close(hit: dict, title: str) -> bool:
    return similarity(core_title(hit.get("trackName", "")), core_title(title)) >= 0.8


def artist_allowed(hit: dict, artist: str, title: str) -> bool:
    """Lyrics of another artist are never used for a track whose artist is known: a cover
    can have nearly the same length, and a song of the same name can be another song.
    Only a track without an artist (or with a channel name, dropped before) is matched
    on its title and length alone."""
    if not artist:
        return True
    name = hit.get("artistName", "")
    if same_artist(name, artist):
        return True
    # "Ryo Fukui" and "福居良": cannot be compared, so the title must be the same
    return different_script(name, artist) and core_title(hit.get("trackName", "")) == core_title(title)


def pick(hits: List[dict], artist: str, title: str, duration: float) -> Optional[Lyrics]:
    """Choose lyrics for a local track.

    Many local files are video versions (radio edit, longer intro or outro), so their
    duration differs from the album version. When artist and title match exactly, a
    gap of up to two minutes is fine and the closest duration wins. When they only look
    alike, the duration must agree within 4 seconds, and the artist must still be the
    same when the track has one.
    """
    best = None
    for h in hits or []:
        gap = abs((h.get("duration") or 0) - duration)
        exact = exact_same(h, artist, title)
        if exact and gap <= SAME_SONG_GAP:
            pass
        elif title_close(h, title) and gap <= STRICT_GAP and artist_allowed(h, artist, title):
            pass
        else:
            continue
        if h.get("instrumental"):
            text, instrumental = "", True
        else:
            text, instrumental = to_text(h), False
            if not text:
                continue
        key = (gap, 0 if exact else 1, 0 if h.get("syncedLyrics") else 1)
        if best is None or key < best[0]:
            best = (key, Lyrics(text, int(h.get("id") or 0), h.get("artistName", ""), h.get("trackName", ""),
                                gap, exact, instrumental))
    return best[1] if best else None


class LrcLib:
    def __init__(self, http: Http):
        self.http = http

    def _get(self, path: str, **params) -> object:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        return self.http.get_json(f"{API}{path}?{q}")

    def find(self, parsed: Parsed, album: str, duration: float) -> Optional[Lyrics]:
        readings = [(a, t) for a, t in parsed.readings if a] or parsed.readings
        a0, t0 = readings[0]
        if a0 and album:
            hit = self._get("get", artist_name=a0, track_name=t0, album_name=album, duration=round(duration))
            if isinstance(hit, dict):
                found = pick([hit], a0, t0, duration)
                if found:
                    return found
        for artist, title in readings:
            hits = self._get("search", track_name=core_or(title), artist_name=artist)
            found = pick(hits if isinstance(hits, list) else [], artist, title, duration)
            if found:
                return found
        artist, title = readings[0]
        hits = self._get("search", q=f"{artist} {core_or(title)}".strip())
        found = pick(hits if isinstance(hits, list) else [], artist, title, duration)
        if found:
            return found
        # Last try: main artist only, title without anything in brackets.
        main = primary_artist(artist)
        bare = BRACKET.sub(" ", split_featuring(title)[0]).strip()
        if main and (main != artist or bare != title):
            hits = self._get("search", track_name=bare, artist_name=main)
            return pick(hits if isinstance(hits, list) else [], main, bare, duration)
        return None


def core_or(title: str) -> str:
    """The title without brackets and featuring, in its original spelling."""
    t, _ = split_featuring(title)
    t = BRACKET.sub(" ", t)
    return " ".join(t.split()) or title
