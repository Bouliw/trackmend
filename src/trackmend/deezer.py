"""Finding a local track in the public Deezer catalogue (no account or key needed)."""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .net import Http
from .text import (Parsed, compact, core_title, different_script, norm, part_numbers, qualifiers_in,
                   same_artist, same_title, similarity, strip_gloss, title_qualifiers)

API = "https://api.deezer.com/"

STRONG = 6      # seconds: same recording
MEDIUM = 45     # seconds: accepted only with exactly the same title
TITLE_ONLY = 2  # seconds: second pass for tracks without any artist
SCRIPT = 3      # seconds: artist written in another script (needs a second opinion)
DOUBTFUL = {"title", "script"}  # tiers that must be confirmed or reviewed before writing

_COMPILATION = re.compile(
    r"\b(greatest hits|best of|the best|the hits|hits|collection|anthology|essentials?|compilation|"
    r"now that s|playlist|workout|party|ann[eé]es|gold|jukebox|top \d+|\d+ (?:songs|tracks))\b"
)


_BRACKET_CHARS = re.compile(r"[()\[\]{}【】（）]")


def _quota(status: int, body: bytes) -> bool:
    """Deezer answers HTTP 200 with {"error": {"code": 4}} when the quota is exceeded."""
    if status != 200 or b'"error"' not in body[:200]:
        return False
    try:
        return json.loads(body).get("error", {}).get("code") == 4
    except ValueError:
        return False


@dataclass
class Match:
    tier: str                      # "strong", "medium" or "title"
    deezer_id: int
    title: str
    artist: str
    album: str = ""
    album_artist: str = ""
    year: str = ""
    genre: str = ""
    track: int = 0
    track_total: int = 0
    disc: int = 0
    cover_url: str = ""
    duration: int = 0
    compilation: bool = False
    gap: float = 0.0

    def fields(self) -> Dict[str, object]:
        f = {"title": self.title, "artist": self.artist, "album": self.album,
             "album_artist": self.album_artist, "year": self.year, "genre": self.genre,
             "track": self.track, "track_total": self.track_total, "disc": self.disc}
        if self.compilation:
            f.pop("year")
            f.pop("genre")
        return {k: v for k, v in f.items() if v not in (None, "", 0)}


@dataclass
class Lookup:
    match: Optional[Match] = None
    reason: str = ""
    rejected_versions: List[str] = field(default_factory=list)


class Deezer:
    def __init__(self, http: Http, language: str = "en"):
        self.http = http
        self.language = language

    def _get(self, path: str) -> Optional[dict]:
        d = self.http.get_json(API + path, headers={"Accept-Language": self.language}, retry_if=_quota)
        if not isinstance(d, dict) or d.get("error"):
            return None
        return d

    def search(self, query: str, limit: int = 10) -> List[dict]:
        q = urllib.parse.quote(query.strip()[:120])
        d = self._get(f"search?limit={limit}&q={q}")
        return list((d or {}).get("data") or [])

    def track(self, tid: int) -> dict:
        return self._get(f"track/{tid}") or {}

    def album(self, aid: int) -> dict:
        return self._get(f"album/{aid}") or {}

    def album_cover(self, artist: str, album: str) -> str:
        """Artwork URL of an album found by artist and title, or ""."""
        if not album:
            return ""
        q = urllib.parse.quote(f"{artist} {album}".strip()[:120])
        d = self._get(f"search/album?limit=10&q={q}")
        for a in (d or {}).get("data") or []:
            if same_artist((a.get("artist") or {}).get("name", ""), artist) and \
                    (norm(a.get("title")) == norm(album) or similarity(a.get("title", ""), album) >= 0.9):
                return a.get("cover_xl") or ""
        return ""

    def any_version_cover(self, artist: str, title: str) -> str:
        """Artwork of the same song in any version (live, remix, other length), or ""."""
        for c in self.search(f"{artist} {title}"):
            if same_artist((c.get("artist") or {}).get("name", ""), artist) and \
                    core_title(c.get("title", "")) == core_title(title):
                url = (c.get("album") or {}).get("cover_xl")
                if url:
                    return url
        return ""

    # -- matching ----------------------------------------------------------------------
    def find(self, parsed: Parsed, duration: float, full_name: str = "") -> Lookup:
        """Best catalogue entry for a local track, following the rules in the README."""
        best: Optional[Tuple[tuple, str, dict]] = None
        rejected: List[str] = []
        local_q = set(parsed.qualifiers)
        # with a known artist, the reading without one is left out: no search by title alone
        readings = [r for r in parsed.readings if r[0]] if parsed.artist else parsed.readings
        for artist, title in readings:
            plain_title = _BRACKET_CHARS.sub(" ", title)
            query = " ".join(f"{artist} {plain_title}".split())
            for c in self.search(query):
                s = score(c, artist, title, duration, full_name or f"{parsed.artist} {parsed.title}", local_q)
                if s is None:
                    continue
                if s[1] == "version":
                    rejected.append(c.get("title", ""))
                    continue
                if best is None or s[0] > best[0]:
                    best = (s[0], s[1], c)
            if best and best[1] == "strong":
                break
        if best is None:
            return Lookup(None, "no match", rejected)
        return Lookup(self._details(best[2], best[1], duration))

    def find_by_title(self, title: str, duration: float) -> Lookup:
        """Second pass for tracks with no artist at all: exact title, duration within 2 s."""
        core = core_title(title)
        if len(core) < 2:
            return Lookup(None, "title too short")
        hits = [c for c in self.search(title, limit=15)
                if same_title(title, c.get("title", ""))
                and abs((c.get("duration") or 0) - duration) <= TITLE_ONLY
                and not _extra_versions(c, set())]
        if not hits:
            return Lookup(None, "no match")
        best = max(hits, key=lambda c: c.get("rank", 0))
        return Lookup(self._details(best, "title", duration))

    def _details(self, c: dict, tier: str, duration: float) -> Match:
        tr = self.track(c["id"]) if c.get("id") else {}
        al_id = (c.get("album") or {}).get("id")
        al = self.album(al_id) if al_id else {}
        genres = [g.get("name", "") for g in ((al.get("genres") or {}).get("data") or [])]
        album_title = al.get("title") or (c.get("album") or {}).get("title") or ""
        compil = al.get("record_type") == "compile" or bool(_COMPILATION.search(norm(album_title)))
        return Match(
            tier=tier,
            deezer_id=int(c.get("id") or 0),
            title=strip_gloss(tr.get("title") or c.get("title") or ""),
            artist=(c.get("artist") or {}).get("name", ""),
            album=album_title,
            album_artist=(al.get("artist") or {}).get("name", ""),
            year=(tr.get("release_date") or al.get("release_date") or "")[:4],
            genre=genres[0] if genres else "",
            track=int(tr.get("track_position") or 0),
            track_total=int(al.get("nb_tracks") or 0),
            disc=int(tr.get("disk_number") or 0),
            cover_url=(c.get("album") or {}).get("cover_xl") or al.get("cover_xl") or "",
            duration=int(c.get("duration") or 0),
            compilation=compil,
            gap=abs((c.get("duration") or 0) - duration),
        )


def candidate_qualifiers(c: dict) -> set:
    q = title_qualifiers(c.get("title", "")) | qualifiers_in(c.get("title_version", ""))
    if "live" in qualifiers_in((c.get("album") or {}).get("title", "")):
        q.add("live")
    return q


def _extra_versions(c: dict, local_q: set) -> set:
    return candidate_qualifiers(c) - local_q


def score(c: dict, artist: str, title: str, duration: float, full_name: str,
          local_q: Optional[set] = None) -> Optional[Tuple[tuple, str]]:
    """Rank a Deezer search result, or None when it must not be used.

    Returns ((tier, original album, title similarity, -gap, popularity), tier name).
    A result that is a different version (live, remix, sped up...) than the local file
    is reported with the tier name "version".
    """
    local_q = set(local_q or ())
    ctitle = c.get("title", "")
    ct, qt = core_title(ctitle), core_title(title)
    if not ct or not qt or part_numbers(ctitle) != part_numbers(title):
        return None
    exact = same_title(title, ctitle)
    ts = 1.0 if exact else similarity(ct, qt)
    if ts < 0.6:
        return None
    cname = (c.get("artist") or {}).get("name", "")
    other_script = False
    if not (same_artist(cname, artist) if artist else artist_in(cname, full_name)):
        # "Ryo Fukui" against "\u798f\u5c45\u826f": the names cannot be compared, so only an exact
        # title and duration are kept, and the match must be confirmed before it is used.
        if not (artist and exact and different_script(cname, artist)):
            return None
        other_script = True
    cq = candidate_qualifiers(c)
    if cq - local_q or local_q - cq:
        return (0,), "version"
    gap = abs((c.get("duration") or 0) - duration)
    album = norm((c.get("album") or {}).get("title", ""))
    original = 0 if _COMPILATION.search(album) else 1
    if other_script:
        if gap > SCRIPT:
            return None
        return (0, original, 1.0, -gap, c.get("rank", 0)), "script"
    if gap <= STRONG:
        tier = "strong"
    elif gap <= MEDIUM and exact and not local_q:
        tier = "medium"
    else:
        return None
    rank = (2 if tier == "strong" else 1, original, round(ts, 1), -gap, c.get("rank", 0))
    return rank, tier


def artist_in(artist: str, text_: str) -> bool:
    """The artist's name appears as whole words in a free text (a track name)."""
    a, t = norm(artist).split(), norm(text_).split()
    if not a or len(compact(artist)) < 3:
        return False
    return any(t[i:i + len(a)] == a for i in range(len(t) - len(a) + 1))


def album_tracks(http: Http, album_id: str) -> Tuple[dict, List[dict]]:
    """Album info and its track list (title, artist, duration), for ``split``."""
    dz = Deezer(http)
    al = dz.album(int(album_id))
    if not al:
        raise ValueError(f"Deezer album {album_id} not found")
    tracks = list((al.get("tracks") or {}).get("data") or [])
    return al, tracks


def album_id_from(s: str) -> str:
    m = re.search(r"album/(\d+)", s) or re.fullmatch(r"\s*(\d+)\s*", s)
    if not m:
        raise ValueError(f"not a Deezer album id or link: {s}")
    return m.group(1)


__all__: Sequence[str] = ("Deezer", "Match", "Lookup", "score", "album_tracks", "album_id_from")
