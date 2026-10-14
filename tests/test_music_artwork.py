"""Artwork must go through Music: after a file write and a refresh, Music keeps showing
the image it had cached. These tests use the test double's model of that cache."""
import os

from conftest import needs_ffmpeg, png_bytes
from trackmend import audio, music as music_mod
from trackmend.music import MusicApp, to_jpeg
from test_matching import dz_track
from test_tags import COVER

JPEG = b"\xff\xd8\xff"


@needs_ffmpeg
def test_png_becomes_jpeg():
    out = to_jpeg(png_bytes(size=16))
    assert out[:3] == JPEG
    assert to_jpeg(COVER) is COVER


@needs_ffmpeg
def test_music_app_reads_a_jpeg_file(monkeypatch):
    seen = {}

    def fake_osa(self, args, js=False):
        seen["args"] = list(args)
        with open(args[3], "rb") as fh:
            seen["data"] = fh.read()
        return "ok"

    monkeypatch.setattr(MusicApp, "_osa", fake_osa)
    app = MusicApp.__new__(MusicApp)                  # no Music needed for this
    app.set_artwork("ABC", png_bytes(size=16))
    assert seen["args"][1:3] == ["set", "ABC"] and seen["args"][3].endswith(".jpg")
    assert seen["data"][:3] == JPEG
    assert not os.path.exists(seen["args"][3])        # temporary file removed


def test_script_reads_jpeg_only():
    path = os.path.join(os.path.dirname(music_mod.__file__), "osa", "artwork.applescript")
    script = open(path, encoding="utf-8").read()
    assert "as JPEG picture" in script and "PNGf" not in script
    assert "set data of artwork 1 of t to img" in script


def test_refresh_alone_keeps_the_old_artwork_on_screen(make_audio, music):
    p = make_audio("old.mp3", title="Song", artist="Band", cover=True)
    pid = music.add_file(p)
    audio.write(p, {}, COVER)
    music.refresh([pid])
    assert audio.cover_bytes(p) == COVER               # the file has the new image
    assert music.shown_artwork(pid) == png_bytes()      # Music still shows the old one
    music.set_artwork(pid, COVER)
    assert music.shown_artwork(pid) == COVER


@needs_ffmpeg
def test_png_artwork_from_a_source_is_shown_as_jpeg(make_audio, music, run_cli, web):
    p = make_audio("h.mp3", title="HUMBLE. (Official Video)", artist="KendrickLamarVEVO")
    pid = music.add_file(p, duration=180.0)
    big_png = png_bytes(size=1000)
    web.routes.update({
        "search?": {"data": [dz_track(1, "HUMBLE.", "Kendrick Lamar", 177, album="DAMN.", album_id=55)]},
        "track/1": {"title": "HUMBLE."},
        "album/55": {"title": "DAMN.", "artist": {"name": "Kendrick Lamar"}},
        "cdn/1.jpg": (200, big_png),
    })
    code, out = run_cli("tags", "--apply", "--write-via", "files")
    assert code == 0, out
    shown = music.shown_artwork(pid)
    assert shown and shown[:3] == JPEG


def test_write_via_music_sets_the_artwork_in_music(make_audio, music, run_cli, web):
    p = make_audio("h.mp3", title="HUMBLE. (Official Video)", artist="KendrickLamarVEVO", cover=True)
    pid = music.add_file(p, duration=180.0)
    web.routes.update({
        "search?": {"data": [dz_track(1, "HUMBLE.", "Kendrick Lamar", 177, album="DAMN.", album_id=55)]},
        "track/1": {"title": "HUMBLE."},
        "album/55": {"title": "DAMN.", "artist": {"name": "Kendrick Lamar"}},
        "cdn/1.jpg": (200, COVER),
    })
    run_cli("tags", "--apply", "--write-via", "music", "--replace-artwork")
    assert music.shown_artwork(pid) == COVER
    run_cli("undo", "--apply")
    shown = music.shown_artwork(pid)                  # the old PNG, given back to Music as JPEG
    assert shown and shown[:3] == JPEG and shown != COVER
