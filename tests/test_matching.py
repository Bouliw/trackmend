
import pytest

from fakes import FakeHttp
from trackmend import deezer, lrclib, text
from trackmend.net import Http, NetError


def dz_track(tid, title, artist, duration, album="Album", rank=1000, album_id=None, title_version=""):
    return {"id": tid, "title": title, "title_version": title_version, "duration": duration, "rank": rank,
            "artist": {"name": artist}, "album": {"id": album_id or tid * 10, "title": album,
                                                  "cover_xl": f"https://cdn/{tid}.jpg"}}


def score(c, name, artist, duration):
    p = text.parse(name, artist)
    return deezer.score(c, p.artist, p.title, duration, f"{artist} {name}", set(p.qualifiers))


def test_strong_and_medium_tiers():
    c = dz_track(1, "HUMBLE.", "Kendrick Lamar", 177)
    assert score(c, "HUMBLE. (Official Video)", "KendrickLamarVEVO", 180)[1] == "strong"
    assert score(c, "HUMBLE. (Official Video)", "KendrickLamarVEVO", 215)[1] == "medium"
    assert score(c, "HUMBLE. (Official Video)", "KendrickLamarVEVO", 240) is None


def test_medium_needs_the_exact_title():
    c = dz_track(1, "Humble Pie", "Kendrick Lamar", 177)
    assert score(c, "Humble", "Kendrick Lamar", 200) is None


def test_wrong_artist_is_rejected():
    assert score(dz_track(1, "Hello", "Lionel Richie", 250), "Hello", "Adele", 250) is None


@pytest.mark.parametrize("cand", [
    dz_track(1, "Song (Live)", "Band", 200),
    dz_track(2, "Song - Live at Wembley", "Band", 200),
    dz_track(3, "Song", "Band", 200, album="Live in Paris"),
    dz_track(4, "Song (Remix)", "Band", 200),
    dz_track(5, "Song (Sped Up)", "Band", 200),
    dz_track(6, "Song", "Band", 200, title_version="(Acoustic)"),
    dz_track(7, "Song (Radio Edit)", "Band", 200),
])
def test_other_versions_never_replace_a_studio_track(cand):
    assert score(cand, "Song", "Band", 200)[1] == "version"


@pytest.mark.parametrize("name", ["Song (Live at Electric Lady)", "Song | Garage Session", "Song (Acoustic)",
                                  "Song - Demo", "Song (Cover)"])
def test_a_local_version_is_not_turned_into_the_studio_one(name):
    assert score(dz_track(1, "Song", "Band", 200), name, "Band", 200)[1] == "version"


def test_same_version_on_both_sides_is_fine():
    c = dz_track(1, "Song (Live)", "Band", 200, album="Live at the Hall")
    assert score(c, "Song (Live) [HD]", "Band", 202)[1] == "strong"


def test_original_album_beats_compilation():
    comp = dz_track(1, "Song", "Band", 200, album="Greatest Hits", rank=999999)
    orig = dz_track(2, "Song", "Band", 201, album="First Album", rank=10)
    assert score(orig, "Song", "Band", 200)[0] > score(comp, "Song", "Band", 200)[0]


def routes_for_humble():
    return {
        "search?": {"data": [dz_track(1, "HUMBLE.", "Kendrick Lamar", 177, album="DAMN.", album_id=55)]},
        "track/1": {"id": 1, "title": "HUMBLE.", "release_date": "2017-03-30", "track_position": 8, "disk_number": 1},
        "album/55": {"id": 55, "title": "DAMN.", "artist": {"name": "Kendrick Lamar"}, "nb_tracks": 14,
                     "record_type": "album", "genres": {"data": [{"name": "Rap/Hip Hop"}]}},
    }


def make_http(routes, **kw):
    t = FakeHttp(routes, **kw)
    return Http(None, transport=t, sleep=lambda s: None), t


def test_find_fills_every_field():
    http, _ = make_http(routes_for_humble())
    res = deezer.Deezer(http).find(text.parse("HUMBLE. (Official Video)", "KendrickLamarVEVO"), 180)
    m = res.match
    assert m and m.tier == "strong"
    assert m.fields() == {"title": "HUMBLE.", "artist": "Kendrick Lamar", "album": "DAMN.",
                          "album_artist": "Kendrick Lamar", "year": "2017", "genre": "Rap/Hip Hop",
                          "track": 8, "track_total": 14, "disc": 1}
    assert m.cover_url == "https://cdn/1.jpg"


def test_quota_errors_are_retried():
    answers = [{"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}}] * 2
    routes = routes_for_humble()
    good = routes["search?"]

    def search(url):
        return answers.pop() if answers else good

    routes["search?"] = search
    http, t = make_http(routes)
    res = deezer.Deezer(http).find(text.parse("HUMBLE.", "Kendrick Lamar"), 177)
    assert res.match is not None
    assert t.count("search?") == 3


def test_cache_avoids_asking_twice(tmp_path):
    t = FakeHttp(routes_for_humble())
    http = Http(str(tmp_path / "c.sqlite"), transport=t, sleep=lambda s: None)
    for _ in range(3):
        deezer.Deezer(http).find(text.parse("HUMBLE.", "Kendrick Lamar"), 177)
    assert t.count("search?") == 1
    http2 = Http(str(tmp_path / "c.sqlite"), transport=t, sleep=lambda s: None)
    deezer.Deezer(http2).find(text.parse("HUMBLE.", "Kendrick Lamar"), 177)
    assert t.count("search?") == 1


def test_server_errors_end_in_neterror():
    http, _ = make_http({}, default=(503, b""))
    with pytest.raises(NetError):
        http.get_json("https://api.deezer.com/search?q=x")


def test_find_by_title_is_strict():
    http, _ = make_http({"search?": {"data": [dz_track(9, "Lonely Song", "Someone", 181, rank=5),
                                               dz_track(8, "Lonely Song", "Other", 190, rank=50)]},
                         "track/9": {}, "album/90": {}})
    m = deezer.Deezer(http).find_by_title("Lonely Song", 180).match
    assert m and m.deezer_id == 9 and m.tier == "title"
    assert deezer.Deezer(http).find_by_title("Lonely Song", 186).match is None


def test_album_id_from_link():
    assert deezer.album_id_from("https://www.deezer.com/fr/album/408695077") == "408695077"
    assert deezer.album_id_from("123") == "123"
    with pytest.raises(ValueError):
        deezer.album_id_from("nope")


# -- LRCLIB ----------------------------------------------------------------------------

def hit(i, artist, title, duration, plain="la la la", synced=None, instrumental=False):
    return {"id": i, "artistName": artist, "trackName": title, "albumName": "A", "duration": duration,
            "instrumental": instrumental, "plainLyrics": plain, "syncedLyrics": synced}


def test_same_song_accepts_two_minutes_and_takes_the_closest():
    hits = [hit(1, "Band", "Song", 300), hit(2, "Band", "Song (feat. X)", 250), hit(3, "Band", "Song", 120)]
    got = lrclib.pick(hits, "Band", "Song", 330)
    assert got.lrclib_id == 1 and got.exact and got.gap == 30


def test_same_song_beyond_two_minutes_is_refused():
    assert lrclib.pick([hit(1, "Band", "Song", 200)], "Band", "Song", 330) is None


def test_close_title_needs_close_duration():
    hits = [hit(1, "Other Band", "Song", 210)]
    assert lrclib.pick(hits, "Band", "Song", 214) is not None
    assert lrclib.pick(hits, "Band", "Song", 220) is None


def test_featured_artists_and_spelling_do_not_matter():
    hits = [hit(1, "Daft Punk feat. Pharrell Williams", "Get Lucky (Radio Edit)", 248)]
    assert lrclib.pick(hits, "Daft Punk", "Get Lucky", 360).exact


def test_instrumental_and_synced_only():
    assert lrclib.pick([hit(1, "B", "S", 100, plain=None, instrumental=True)], "B", "S", 100).instrumental
    got = lrclib.pick([hit(2, "B", "S", 100, plain=None, synced="[00:01.00] one\n[00:02.50] two")], "B", "S", 100)
    assert got.text == "one\ntwo"


def test_lrclib_find_uses_get_then_search():
    routes = {"api/get?": (404, b""), "api/search?": [hit(5, "Kendrick Lamar", "HUMBLE.", 177)]}
    http, t = make_http(routes)
    got = lrclib.LrcLib(http).find(text.parse("HUMBLE. (Official Video)", "KendrickLamarVEVO"), "DAMN.", 211)
    assert got and got.lrclib_id == 5
    assert t.count("api/get?") == 1
    assert "track_name=HUMBLE." in t.calls[1]


# -- a known artist is never replaced (rule: covers can have nearly the same length) -----

def test_known_artist_is_never_replaced_by_a_cover(make_audio, music, run_cli, web):
    from trackmend import audio
    p = make_audio("fg.mp3", title="Les gens bien élevés", artist="France Gall")
    pid = music.add_file(p, duration=105.0)
    cover = dz_track(5, "Les gens bien élevés", "Alka Balbir", 105, album="La première fois", album_id=50)
    web.routes.update({"api.deezer.com/search?": {"data": [cover]}, "track/5": {"title": "Les gens bien élevés"},
                       "album/50": {"title": "La première fois"}})
    code, out = run_cli("tags", "--apply", "--all")
    assert code == 0, out
    assert audio.read(p).artist == "France Gall" and music.get(pid).artist == "France Gall"
    assert "Alka Balbir" not in out and "La première fois" not in out
    assert not [u for u in web.calls if "&q=Les" in u]            # no search by title alone


def test_title_only_search_is_for_tracks_without_a_real_artist(make_audio, music, run_cli, web):
    p = make_audio("v.mp3", title="Venus", artist="Inconnu")
    music.add_file(p, duration=197.0)
    web.routes.update({"api.deezer.com/search?": {"data": [dz_track(6, "Venus", "Band", 197, album="A", album_id=60)]},
                       "track/6": {"title": "Venus"}, "album/60": {"title": "A"}})
    run_cli("tags", "--apply")
    assert [u for u in web.calls if "&q=Venus" in u]              # the channel name was not an artist


def test_lyrics_of_another_artist_are_never_used():
    fg = [hit(1, "Alka Balbir", "Les gens bien élevés", 105, plain="cover")]
    assert lrclib.pick(fg, "France Gall", "Les gens bien élevés", 105) is None
    assert lrclib.pick(fg, "", "Les gens bien élevés", 105).text == "cover"          # no artist: title and length
    own = fg + [hit(2, "France Gall", "Les Gens Bien Elevés", 104, plain="original")]
    assert lrclib.pick(own, "France Gall", "Les gens bien élevés", 105).text == "original"
    fukui = [hit(3, "福居良", "Scenery", 301, plain="(jazz)")]
    assert lrclib.pick(fukui, "Ryo Fukui", "Scenery", 300).text == "(jazz)"         # same name, other script
