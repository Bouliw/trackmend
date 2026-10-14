"""Second opinions for matches that cannot be trusted on their own.

Two kinds of Deezer matches are doubtful:

* ``script``: the catalogue writes the artist in another script (``福居良`` for
  ``Ryo Fukui``). Same title and same length are not enough: that is how ``Joji`` can end
  up as ``Gabry Ponte``.
* ``title``: the local file has no artist at all.

They are used only when something else agrees: the same album is already in the
library, MusicBrainz knows the local name as a name of that artist, or iTunes has the
track under the local name with the same length. Otherwise they wait for
``trackmend review``.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Tuple

from .artwork import ITunes
from .deezer import Match
from .music import Track
from .musicbrainz import MusicBrainz
from .net import Http, NetError
from .text import core_title, norm, same_artist

ITUNES_GAP = 3.0


class Confirmer:
    def __init__(self, http: Http, library: List[Track], itunes: bool = True, musicbrainz: bool = True):
        self.itunes = ITunes(http) if itunes else None
        self.mb = MusicBrainz(http) if musicbrainz else None
        self.albums: Dict[str, List[Track]] = defaultdict(list)
        for t in library:
            if t.album.strip():
                self.albums[norm(t.album)].append(t)

    def check(self, match: Match, local_artist: str, title: str, duration: float,
              track: Track) -> Tuple[bool, str]:
        """(confirmed, why)."""
        album = norm(match.album)
        if album and norm(track.album) == album:
            return True, "the file already names this album"
        for other in self.albums.get(album, []):
            if other.pid != track.pid and (same_artist(other.artist, match.artist) or
                                           (local_artist and same_artist(other.artist, local_artist))):
                return True, "same album already in the library"
        if match.tier == "script" and local_artist:
            try:
                if self.mb and any(same_artist(n, local_artist) for n in self.mb.artist_names(match.artist)):
                    return True, f"MusicBrainz knows {match.artist} as {local_artist}"
                if self.itunes and self.on_itunes(local_artist, title, duration):
                    return True, "iTunes has the same track under that name"
            except NetError:
                pass
            return False, f"artist written differently in the catalogue ({match.artist})"
        return False, "found by title and length only (no artist in the file)"

    def on_itunes(self, artist: str, title: str, duration: float) -> bool:
        assert self.itunes is not None
        want = core_title(title)
        for store in self.itunes.stores:
            for r in self.itunes._search(f"{artist} {title}", "song", store):
                length = (r.get("trackTimeMillis") or 0) / 1000
                if core_title(r.get("trackName", "")) == want and abs(length - duration) <= ITUNES_GAP \
                        and same_artist(r.get("artistName", ""), artist):
                    return True
        return False

