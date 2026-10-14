import os

import pytest

from conftest import ffmpeg, needs_ffmpeg, png_bytes
from fakes import FakeHttp, FakeMusic
from trackmend import artwork, audio, lrclib, music as music_mod, ops, text
from trackmend.net import Http, NetError
from trackmend.tags import keep_artist, keep_featuring
from test_matching import dz_track, hit
from test_tags import COVER


def jpeg(tmp_path, w, h, bars=0, name="img.jpg"):
    path = str(tmp_path / name)
    vf = f"color=c=red:s={w}x{h}"
    if bars:
        vf += (f",drawbox=x=0:y=0:w={w}:h={bars}:color=black:t=fill"
               f",drawbox=x=0:y={h - bars}:w={w}:h={bars}:color=black:t=fill")
    ffmpeg("-f", "lavfi", "-i", vf, "-frames:v", "1", path)
    return open(path, "rb").read()


# -- artwork geometry ------------------------------------------------------------------

@needs_ffmpeg
def test_image_size_and_square(tmp_path):
    assert artwork.image_size(png_bytes(size=8)) == (8, 8)
    assert artwork.is_square(png_bytes())
    frame = jpeg(tmp_path, 640, 360)
    assert artwork.image_size(frame) == (640, 360) and not artwork.is_square(frame)
    assert artwork.image_size(b"nope") is None


@needs_ffmpeg
def test_only_4_3_and_16_9_pictures_count_as_video_frames(tmp_path):
    assert artwork.is_video_frame(jpeg(tmp_path, 1280, 720, name="a.jpg"))
    assert artwork.is_video_frame(jpeg(tmp_path, 640, 480, name="b.jpg"))
    assert not artwork.is_video_frame(jpeg(tmp_path, 1000, 904, name="c.jpg"))  # a slightly uneven scan
    assert not artwork.is_video_frame(png_bytes())


@needs_ffmpeg
def test_uneven_scan_of_a_sleeve_is_left_alone(make_audio, music, run_cli, web, tmp_path):
    p = make_audio("s.mp3", title="Mais Que Nada", artist="Sergio Mendes", album="Herb Alpert Presents")
    scan = jpeg(tmp_path, 1000, 919)
    audio.write(p, {}, scan)
    music.add_file(p, duration=200.0)
    code, out = run_cli("tags")
    assert "0 tracks to look up" in out
    assert audio.cover_bytes(p) == scan


@needs_ffmpeg
def test_video_frame_loses_its_black_bars_then_becomes_square(tmp_path):
    frame = jpeg(tmp_path, 1280, 720, bars=90)
    out = artwork.squarify(frame)
    assert artwork.image_size(out) == (540, 540)


@needs_ffmpeg
def test_4_3_frame_without_bars(tmp_path):
    out = artwork.squarify(jpeg(tmp_path, 640, 480))
    assert artwork.image_size(out) == (480, 480)


# -- artwork search chain --------------------------------------------------------------

def finder_for(music, routes, tmp_path):
    t = FakeHttp(routes)
    http = Http(None, transport=t, sleep=lambda s: None)
    return artwork.ArtworkFinder(http, str(tmp_path / "covers"), music.tracks()), t


def test_library_sibling_comes_first(make_audio, music, tmp_path):
    a = make_audio("a.mp3", title="One", artist="Band", album="Record", cover=True)
    b = make_audio("b.mp3", title="Two", artist="Band", album="Record")
    music.add_file(a)
    pb = music.add_file(b)
    finder, web = finder_for(music, {}, tmp_path)
    data, where, doubtful = finder.find(music.get(pb), "Band", "Two", "Record")
    assert data == png_bytes() and where == "same album in the library" and not doubtful
    assert not web.calls


def test_album_search_then_itunes_then_other_version(music, tmp_path):
    t = music_mod.Track(pid="X", name="Song", artist="Band", album="Record")
    routes = {"api.deezer.com/search/album": {"data": []},
              "itunes.apple.com/search": {"results": [{"artistName": "Band", "collectionName": "Record",
                                                        "artworkUrl100": "https://is1/a/100x100bb.jpg"}]},
              "is1/a/1000x1000bb.jpg": (200, COVER)}
    finder, web = finder_for(music, routes, tmp_path)
    data, where, _ = finder.find(t, "Band", "Song", "Record")
    assert data == COVER and where == "album on iTunes"
    assert web.count("country=us") == 1 and web.count("entity=album") == 1

    routes2 = {"search/album": {"data": []}, "itunes.apple.com": {"results": []},
               "api.deezer.com/search?": {"data": [dz_track(4, "Song (Live)", "Band", 300, album="Live")]},
               "cdn/4.jpg": (200, COVER)}
    finder, web = finder_for(music, routes2, tmp_path)
    data, where, _ = finder.find(t, "Band", "Song", "Record")
    assert where == "same song, other version, on Deezer"
    assert web.count("country=jp") == 1  # every store was tried first


def test_live_versions_never_borrow_other_artwork(music, tmp_path):
    t = music_mod.Track(pid="X", name="Song (Live)", artist="Band")
    routes = {"api.deezer.com/search?": {"data": [dz_track(4, "Song", "Band", 300)]}, "cdn/4.jpg": (200, COVER)}
    finder, _ = finder_for(music, routes, tmp_path)
    assert finder.find(t, "Band", "Song (Live)", "", ["live"]) == (None, "", False)


def test_itunes_403_pauses_then_goes_on():
    answers = [(403, b""), (403, b""), (200, b'{"results": []}')]
    pauses = []
    t = FakeHttp({"itunes.apple.com": lambda url: answers.pop(0)})
    http = Http(None, transport=t, sleep=pauses.append)
    assert http.get_json("https://itunes.apple.com/search?term=x") == {"results": []}
    assert [p for p in pauses if p >= 60] == [60, 120]


def test_lrclib_503_is_tried_again_quickly():
    # LRCLIB's 503s are random overload answers, not a limit per address: short pauses.
    pauses = []
    t = FakeHttp({"lrclib.net": (503, b"")})
    http = Http(None, transport=t, sleep=pauses.append)
    with pytest.raises(NetError):
        http.get_json("https://lrclib.net/api/search?q=x")
    waits = [p for p in pauses if p >= 1]
    assert waits == [1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5]
    assert t.count("lrclib.net") == 11           # the first try and ten more


def test_lrclib_503_then_an_answer():
    answers = iter([(503, b""), (503, b""), (200, b"[]")])
    pauses = []
    http = Http(None, transport=lambda url, h: next(answers), sleep=pauses.append)
    assert http.get_json("https://lrclib.net/api/search?q=y") == []
    assert [p for p in pauses if p >= 1] == [1.5, 2.5]


# -- artists, titles, genres -----------------------------------------------------------

def test_keep_artist():
    assert keep_artist("Roméo Elvis, Le Motel", "Roméo Elvis", False) == "Roméo Elvis, Le Motel"
    assert keep_artist("KoЯn", "Korn", False) == "KoЯn"
    assert keep_artist("KendrickLamar", "Kendrick Lamar", True) == "Kendrick Lamar"
    assert keep_artist("beyonce", "Beyoncé", False) == "Beyoncé"
    assert keep_artist("Someone Else", "Band", False) == "Band"


def test_keep_featuring():
    assert keep_featuring("Song", ["Guest"], "Band") == "Song (feat. Guest)"
    assert keep_featuring("Song (feat. Guest)", ["Guest"], "Band") == "Song (feat. Guest)"
    assert keep_featuring("Song", ["Guest"], "Band & Guest") == "Song"


def test_lost_accents_take_the_catalogue_spelling(make_audio, music, run_cli, web):
    p = make_audio("i.mp3", title="Inachevs", artist="Band")
    music.add_file(p, duration=200.0)
    web.routes.update({"search?": {"data": [dz_track(7, "Inachevés", "Band", 199, album="Album", album_id=70)]},
                       "track/7": {}, "album/70": {"title": "Album"}, "cdn/7.jpg": (200, COVER)})
    code, out = run_cli("tags", "--apply")
    assert audio.read(p).title == "Inachevés"


def test_part_numbers_must_agree(make_audio, music, run_cli, web):
    p = make_audio("p.mp3", title="Saga, Pt. II", artist="Band")
    music.add_file(p, duration=200.0)
    web.routes.update({"search?": {"data": [dz_track(8, "Saga, Pt. III", "Band", 200, album="Album")]},
                       "cdn/8.jpg": (200, COVER)})
    run_cli("tags", "--apply", "--no-artwork")
    assert audio.read(p).album == ""


def test_short_artist_never_matches_everyone(make_audio, music, run_cli, web):
    p = make_audio("o.mp3", title="Song", artist="((( O )))")
    music.add_file(p, duration=200.0)
    web.routes.update({"search?": {"data": [dz_track(9, "Song", "Foo Fighters", 200, album="Album")]},
                       "cdn/9.jpg": (200, COVER)})
    run_cli("tags", "--apply", "--no-artwork")
    assert audio.read(p).artist == "((( O )))" and audio.read(p).album == ""


def test_colors_session_is_never_replaced_by_the_studio_version(make_audio, music, run_cli, web):
    p = make_audio("c.mp3", title="Artist - Song", artist="COLORS")
    music.add_file(p, duration=200.0)
    web.routes.update({"search?": {"data": [dz_track(10, "Song", "Artist", 200, album="Studio Album")]},
                       "cdn/10.jpg": (200, COVER)})
    run_cli("tags", "--apply")
    t = audio.read(p)
    assert (t.artist, t.title, t.album) == ("Artist", "Song", "")
    assert not t.has_cover


def test_youtube_category_genre_is_emptied_in_file_and_music(make_audio, music, run_cli, web):
    p = make_audio("g.mp3", title="Song", artist="Band", album="A", genre="People & Blogs", cover=True)
    pid = music.add_file(p, duration=200.0)
    web.routes["search?"] = {"data": []}
    run_cli("tags", "--apply")
    assert audio.read(p).genre == ""
    assert music.get(pid).genre == ""  # refresh alone would have kept it


def test_album_and_track_number_from_the_name(make_audio, music, run_cli, web):
    p = make_audio("n.mp3", title="[03] Feel The Need (Disco 1979)", artist="Band")
    music.add_file(p, duration=200.0)
    web.routes["search?"] = {"data": []}
    run_cli("tags", "--apply", "--no-artwork")
    t = audio.read(p)
    assert (t.title, t.album, t.year, t.track) == ("Feel The Need", "Disco", "1979", 3)


def test_video_frame_artwork_is_replaced_or_cut(make_audio, music, run_cli, web, tmp_path):
    p = make_audio("v.mp3", title="Rare Song", artist="Nobody Knows", album="Demo Tape")
    frame = jpeg(tmp_path, 1280, 720, bars=90)
    audio.write(p, {}, frame)
    music.add_file(p, duration=200.0)
    web.routes.update({"search?": {"data": []}, "search/album": {"data": []}, "itunes.apple.com": {"results": []}})
    code, out = run_cli("tags", "--apply")
    assert "video frame cut to a square" in out
    assert artwork.image_size(audio.cover_bytes(p)) == (540, 540)


# -- writing through Music --------------------------------------------------------------

def test_write_via_music_and_undo(make_audio, music, run_cli, web):
    p = make_audio("h.mp3", title="HUMBLE. (Official Video)", artist="KendrickLamarVEVO")
    pid = music.add_file(p, duration=180.0)
    web.routes.update({
        "search?": {"data": [dz_track(1, "HUMBLE.", "Kendrick Lamar", 177, album="DAMN.", album_id=55)]},
        "track/1": {"release_date": "2017-03-30", "track_position": 8},
        "album/55": {"title": "DAMN.", "artist": {"name": "Kendrick Lamar"}, "nb_tracks": 14},
        "cdn/1.jpg": (200, COVER),
    })
    code, out = run_cli("tags", "--apply", "--write-via", "music")
    assert code == 0, out
    kinds = [c[0] for c in music.calls]
    assert "set" in kinds and "set_artwork" in kinds and "refresh" not in kinds
    assert music.get(pid).album == "DAMN." and audio.cover_bytes(p) == COVER
    code, out = run_cli("undo", "--apply")
    assert code == 0, out
    t = music.get(pid)
    assert (t.name, t.album, t.year) == ("HUMBLE. (Official Video)", "", 0)
    assert not audio.read(p).has_cover


# -- lyrics ------------------------------------------------------------------------------

def test_lyrics_retry_with_main_artist_and_bare_title():
    calls = []

    def search(url):
        calls.append(url)
        if "artist_name=Daft+Punk&" in url + "&" and "track_name=Get+Lucky&" in url + "&":
            return [hit(1, "Daft Punk", "Get Lucky", 248, plain="words")]
        return []

    t = FakeHttp({"api/search": search, "api/get": (404, b"")})
    http = Http(None, transport=t, sleep=lambda s: None)
    got = lrclib.LrcLib(http).find(text.parse("Get Lucky [Radio Edit]", "Daft Punk & Pharrell Williams"), "", 250)
    assert got and got.text == "words"


def test_single_track_lyrics_are_written_at_once(make_audio, music, run_cli, web):
    a = make_audio("a.mp3", title="Alpha", artist="Band")
    b = make_audio("b.mp3", title="Beta", artist="Band")
    pa = music.add_file(a, duration=200.0)
    pb = music.add_file(b, duration=200.0)
    web.routes["lrclib.net/api/search"] = lambda url: [hit(1, "Band", "Alpha", 200, plain="alpha words")]
    code, out = run_cli("lyrics", "--track", "Alpha", "--apply")
    assert code == 0, out
    assert music.lyrics(pa) == "alpha words" and music.lyrics(pb) == ""
    code, out = run_cli("lyrics", "--track", "Alpha", "--apply")
    assert "already has lyrics" in out


# -- Music cannot read the Desktop -------------------------------------------------------

def test_files_on_the_desktop_are_moved_before_adding(tmp_path, monkeypatch, make_audio, media):
    monkeypatch.setenv("HOME", str(tmp_path))
    desk = tmp_path / "Desktop"
    p = make_audio("song.mp3", folder=str(desk), title="S")
    assert not music_mod.readable_by_music(p)
    fake = FakeMusic(media)
    ctx = ops.Context(music=fake, home=__import__("trackmend.store", fromlist=["Home"]).Home(str(tmp_path / "h")))
    run = ctx.home.new_run("t", [])
    (path, pid), = ops.add_files(ctx, run, [p])
    assert pid and path.startswith(str(tmp_path / "Music" / "trackmend"))
    assert not os.path.exists(p)


def test_each_export_uses_its_own_file(monkeypatch):
    seen = []
    real = music_mod.tempfile.TemporaryDirectory

    class Spy(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            seen.append(self.name)

    monkeypatch.setattr(music_mod.tempfile, "TemporaryDirectory", Spy)
    monkeypatch.setattr(music_mod.sys, "platform", "darwin")
    app = music_mod.MusicApp()
    monkeypatch.setattr(app, "_osa", lambda args, js=False: open(args[1], "w").write("[]") and "0")
    app.tracks()
    app.tracks()
    assert len(set(seen)) == 2
