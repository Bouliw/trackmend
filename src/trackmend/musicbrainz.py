"""MusicBrainz (artist names in every script, releases) and the Cover Art Archive."""
from __future__ import annotations

import re
import urllib.parse
from typing import List

from .net import Http
from .text import norm, same_artist, similarity

API = "https://musicbrainz.org/ws/2/"
COVER = "https://coverartarchive.org/release/{}/front-1200"
MIN_SCORE = 85


def _quote(s: str) -> str:
    """Lucene-safe value for a MusicBrainz query."""
    return re.sub(r'[\\"+\-!(){}\[\]^~*?:/]|&&|\|\|', " ", s or "").strip()


def _unsort(name: str) -> str:
    """``Fukui, Ryo`` -> ``Ryo Fukui``."""
    last, sep, first = (name or "").partition(", ")
    return f"{first} {last}" if sep and first else name


class MusicBrainz:
    def __init__(self, http: Http):
        self.http = http

    def _get(self, entity: str, query: str, limit: int = 5) -> dict:
        q = urllib.parse.urlencode({"query": query, "fmt": "json", "limit": limit})
        d = self.http.get_json(f"{API}{entity}?{q}")
        return d if isinstance(d, dict) else {}

    def artist_names(self, name: str) -> List[str]:
        """Every name MusicBrainz knows for an artist: ``福居良`` -> ``Ryo Fukui``, ``Fukui, Ryo``..."""
        if not _quote(name):
            return []
        d = self._get("artist", f'artist:"{_quote(name)}" OR alias:"{_quote(name)}"')
        out: List[str] = []
        for a in d.get("artists") or []:
            if int(a.get("score") or 0) < MIN_SCORE:
                continue
            out += [a.get("name") or "", _unsort(a.get("sort-name") or "")]
            for alias in a.get("aliases") or []:
                out += [alias.get("name") or "", _unsort(alias.get("sort-name") or "")]
        return [n for n in out if n]

    def cover_urls(self, artist: str, album: str) -> List[str]:
        """Cover Art Archive links (1200 px) for releases of this album."""
        if not (_quote(artist) and _quote(album)):
            return []
        d = self._get("release", f'release:"{_quote(album)}" AND artist:"{_quote(artist)}"')
        urls = []
        for r in d.get("releases") or []:
            if int(r.get("score") or 0) < MIN_SCORE:
                continue
            credit = " ".join(c.get("name", "") + c.get("joinphrase", "") for c in r.get("artist-credit") or [])
            title = r.get("title", "")
            if not same_artist(credit, artist):
                continue
            if norm(title) != norm(album) and similarity(title, album) < 0.9:
                continue
            urls.append(COVER.format(r["id"]))
        return urls
