import json
import os

from conftest import png_bytes
from fakes import FakeHttp
from trackmend import audio, deezer, musicbrainz, text, youtube
from trackmend.net import Http
from test_matching import dz_track
from test_tags import COVER

FUKUI = "福居良"


def scenery_routes(web, extra=None):
    web.routes.update({
        "api.deezer.com/search?": {"data": [dz_track(20, "Scenery", FUKUI, 300, album="Scenery", album_id=200)]},
        "track/20": {"title": "Scenery", "release_date": "1976-01-01", "track_position": 3},
        "album/200": {"title": "Scenery", "artist": {"name": FUKUI}, "nb_tracks": 6},
        "cdn/20.jpg": (200, COVER),
        **(extra or {}),
    })


def test_script_tier_needs_an_exact_title_and_length():
    c = dz_track(1, "Scenery", FUKUI, 300)
    p = text.parse("Ryo Fukui - Scenery", "")
    assert deezer.score(c, p.artist, p.title, 301, "", set())[1] == "script"
    assert deezer.score(c, p.artist, p.title, 306, "", set()) is None
    assert deezer.score(dz_track(2, "Scenery Two", FUKUI, 300), p.artist, p.title, 300, "", set()) is None


def test_other_latin_artist_is_never_accepted():
    c = dz_track(3, "Glimpse of Us", "Gabry Ponte", 233)
    p = text.parse("Joji - Glimpse of Us", "")
    assert deezer.score(c, p.artist, p.title, 233, "", set()) is None


def test_script_match_confirmed_by_musicbrainz_is_written(make_audio, music, run_cli, web):
    p = make_audio("s.mp3", title="Ryo Fukui - Scenery")
    pid = music.add_file(p, duration=301.0)
    scenery_routes(web, {"musicbrainz.org/ws/2/artist": {"artists": [
        {"name": FUKUI, "sort-name": "Fukui, Ryo", "score": 100, "aliases": [{"name": "Ryo Fukui"}]}]}})
    code, out = run_cli("tags", "--apply")
    assert code == 0, out
    assert "confirmed: MusicBrainz knows" in out
    t = audio.read(p)
    assert (t.album, t.year) == ("Scenery", "1976")
    assert music.get(pid).album == "Scenery"


def test_unconfirmed_script_match_waits_for_review(make_audio, music, run_cli, web, home):
    p = make_audio("s.mp3", title="Ryo Fukui - Scenery")
    pid = music.add_file(p, duration=301.0)
    scenery_routes(web)  # MusicBrainz and iTunes know nothing
    code, out = run_cli("tags", "--apply")
    assert "to review: artist written differently" in out
    assert audio.read(p).album == ""          # nothing written
    store = json.load(open(os.path.join(home, "review", "review.json")))
    assert store[pid]["status"] == "pending" and store[pid]["changes"]["album"] == "Scenery"
    code, out = run_cli("tags", "--apply")
    assert "0 tracks to look up" in out      # not proposed twice

    code, out = run_cli("review")
    assert "1 doubtful matches" in out and "review.html" in out
    page = open(os.path.join(home, "review", "review.html")).read()
    assert "Ryo Fukui" in page and "proposed" in page
    code, out = run_cli("review", "--accept", "1", "--apply")
    assert code == 0, out
    assert audio.read(p).album == "Scenery" and audio.cover_bytes(p) == COVER
    assert music.shown_artwork(pid) == COVER
    code, out = run_cli("review")
    assert "Nothing to review" in out
    code, out = run_cli("undo", "--apply")
    assert audio.read(p).album == "" and not audio.read(p).has_cover
    assert music.shown_artwork(pid) is None


def test_same_album_in_the_library_confirms(make_audio, music, run_cli, web):
    a = make_audio("a.mp3", title="Early Summer", artist="Ryo Fukui", album="Scenery")
    music.add_file(a, duration=400.0)
    p = make_audio("s.mp3", title="Ryo Fukui - Scenery")
    music.add_file(p, duration=301.0)
    scenery_routes(web)
    code, out = run_cli("tags", "--apply", "--no-artwork")
    assert "confirmed: same album already in the library" in out
    assert audio.read(p).album == "Scenery"


def test_title_only_match_waits_for_review(make_audio, music, run_cli, web):
    p = make_audio("t.mp3", title="Lonely Song")
    music.add_file(p, duration=180.0)
    web.routes.update({"api.deezer.com/search?": {"data": [dz_track(9, "Lonely Song", "Someone", 181)]},
                       "track/9": {}, "album/90": {"title": "Album"}})
    code, out = run_cli("tags", "--apply", "--no-artwork")
    assert "found by title and length only" in out
    assert audio.read(p).artist == ""


def test_rejected_matches_are_not_proposed_again(make_audio, music, run_cli, web):
    p = make_audio("s.mp3", title="Ryo Fukui - Scenery")
    music.add_file(p, duration=301.0)
    scenery_routes(web)
    run_cli("tags", "--apply")
    code, out = run_cli("review", "--reject", "all", "--apply")
    assert "Nothing accepted" in out
    code, out = run_cli("tags", "--apply")
    assert "0 tracks to look up" in out
    assert audio.read(p).album == ""


def test_interactive_review(make_audio, music, run_cli, web, monkeypatch):
    p = make_audio("s.mp3", title="Ryo Fukui - Scenery")
    music.add_file(p, duration=301.0)
    scenery_routes(web)
    run_cli("tags", "--apply")
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    code, out = run_cli("review", interactive=True)
    assert "✓  1." in out
    run_cli("review", "--apply")
    assert audio.read(p).album == "Scenery"


def test_musicbrainz_cover_art_archive(music, tmp_path):
    from trackmend.artwork import ArtworkFinder
    from trackmend.music import Track
    routes = {
        "api.deezer.com": {"data": []}, "itunes.apple.com": {"results": []},
        "musicbrainz.org/ws/2/release": {"releases": [
            {"id": "aaaa-1", "score": 100, "title": "Rare Record",
             "artist-credit": [{"name": "Small Band", "joinphrase": ""}]}]},
        "coverartarchive.org/release/aaaa-1/front-1200": (200, COVER),
    }
    t = FakeHttp(routes)
    finder = ArtworkFinder(Http(None, transport=t, sleep=lambda s: None), str(tmp_path / "c"), [])
    data, where, doubtful = finder.find(Track(pid="X", name="Song"), "Small Band", "Song", "Rare Record")
    assert data == COVER and where == "album on MusicBrainz (Cover Art Archive)" and not doubtful


def test_musicbrainz_names():
    t = FakeHttp({"musicbrainz.org/ws/2/artist": {"artists": [
        {"name": FUKUI, "sort-name": "Fukui, Ryo", "score": 98, "aliases": [{"name": "Ryo Fukui", "sort-name": ""}]},
        {"name": "Someone Else", "sort-name": "Else, Someone", "score": 40}]}})
    names = musicbrainz.MusicBrainz(Http(None, transport=t, sleep=lambda s: None)).artist_names(FUKUI)
    assert "Ryo Fukui" in names and "Someone Else" not in names


def test_youtube_thumbnail_is_proposed_never_written(make_audio, music, run_cli, web, monkeypatch, tmp_path, home):
    p = make_audio("y.mp3", title="Tiny Artist - Basement Song", album="")
    pid = music.add_file(p, duration=200.0)
    monkeypatch.setattr(youtube, "search", lambda q, limit=8: [
        {"id": "abc123", "title": "Tiny Artist - Basement Song (live in my room)", "channel": "Tiny Artist",
         "duration": 260},
        {"id": "xyz789", "title": "Tiny Artist - Basement Song", "channel": "Tiny Artist", "duration": 201}])
    web.routes.update({"api.deezer.com": {"data": []}, "itunes.apple.com": {"results": []},
                       "i.ytimg.com/vi/xyz789/maxresdefault.jpg": (200, COVER)})
    code, out = run_cli("tags", "--apply")
    assert "artwork to review: YouTube thumbnail" in out
    assert audio.read(p).title == "Basement Song"   # the clean-up is written
    assert not audio.read(p).has_cover               # the thumbnail is not
    run_cli("review", "--accept", "all", "--apply")
    assert audio.cover_bytes(p) == COVER
    assert music.get(pid).name == "Basement Song"


def test_youtube_pick_needs_same_length_and_names():
    entries = [{"id": "1", "title": "Other - Song", "channel": "x", "duration": 200},
               {"id": "2", "title": "Band - Song (Official Video)", "channel": "BandVEVO", "duration": 230},
               {"id": "3", "title": "Band - Song", "channel": "Band", "duration": 201}]
    assert youtube.pick(entries, "Band", "Song", 200)["id"] == "3"
    assert youtube.pick(entries, "Band", "Song", 300) is None
    assert png_bytes()
