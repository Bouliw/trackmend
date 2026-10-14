import os

import pytest
from mutagen.id3 import ID3, TXXX
from mutagen.mp4 import MP4, MP4FreeForm

from conftest import png_bytes
from trackmend import audio

FORMATS = [".mp3", ".m4a", ".aiff"]


@pytest.mark.parametrize("ext", FORMATS)
def test_write_and_read_back(make_audio, ext):
    path = make_audio("song" + ext, seconds=2)
    audio.write(path, {"title": "Café 夜", "artist": "Artist", "album": "Album", "album_artist": "AA",
                       "year": "2019", "genre": "Pop", "track": 3, "track_total": 11, "disc": 1},
                png_bytes())
    t = audio.read(path)
    assert (t.title, t.artist, t.album, t.album_artist) == ("Café 夜", "Artist", "Album", "AA")
    assert (t.year, t.genre, t.track, t.track_total, t.disc) == ("2019", "Pop", 3, 11, 1)
    assert t.has_cover
    assert 1.5 < t.length < 2.6
    assert audio.cover_bytes(path) == png_bytes()


@pytest.mark.parametrize("ext", FORMATS)
def test_empty_value_removes_field(make_audio, ext):
    path = make_audio("song" + ext, title="T", album="Album", genre="Rock")
    audio.write(path, {"album": "", "genre": None})
    t = audio.read(path)
    assert t.title == "T" and t.album == "" and t.genre == ""


@pytest.mark.parametrize("ext", FORMATS)
def test_snapshot_restore_is_exact(make_audio, tmp_path, ext):
    path = make_audio("song" + ext, title="Old", artist="Old Artist", track=2, cover=True)
    if ext == ".m4a":
        m = MP4(path)
        m["----:com.apple.iTunes:CUSTOM"] = [MP4FreeForm(b"keep me")]
        m["cpil"] = True
        m.save()
    elif ext == ".mp3":
        tags = ID3(path)
        tags.add(TXXX(encoding=3, desc="CUSTOM", text=["keep me"]))
        tags.save(path, v2_version=3)
    before = audio.read(path)
    raw_before = _raw(path)
    backup = audio.snapshot(path, str(tmp_path / "b1"))
    audio.write(path, {"title": "New", "artist": "New Artist", "album": "Added", "track": 9}, png_bytes((0, 0, 255)))
    assert audio.read(path).title == "New"
    audio.restore(path, backup)
    after = audio.read(path)
    assert after.as_dict() == before.as_dict()
    assert audio.cover_bytes(path) == png_bytes()
    assert _raw(path) == raw_before


@pytest.mark.parametrize("ext", FORMATS)
def test_snapshot_of_untagged_file(make_audio, tmp_path, ext):
    path = make_audio("bare" + ext)
    backup = audio.snapshot(path, str(tmp_path / "b"))
    audio.write(path, {"title": "Added"}, png_bytes())
    audio.restore(path, backup)
    t = audio.read(path)
    assert t.title == "" and not t.has_cover


def test_lyrics(make_audio):
    for ext in (".mp3", ".m4a"):
        path = make_audio("l" + ext)
        audio.write(path, {"lyrics": "line one\nline two"})
        assert audio.read(path).lyrics == "line one\nline two"


def test_unsupported_and_broken(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    assert not audio.is_supported(str(p))
    assert not audio.is_supported(None)
    with pytest.raises(audio.AudioError):
        audio.read(str(p))
    with pytest.raises(audio.AudioError):
        audio.image_mime(b"GIF89a")


def _raw(path):
    """Comparable view of every tag frame or atom."""
    f = audio._open(path)
    if f.tags is None:
        return {}
    if audio.ext(path) in audio.MP4_EXT:
        return {k: [bytes(x) if isinstance(x, bytes) else x for x in v] if isinstance(v, list) else v
                for k, v in f.tags.items()}
    return sorted(repr(fr) for fr in f.tags.values())


def test_snapshot_keeps_id3_version(make_audio, tmp_path):
    path = make_audio("v4.mp3", title="x")
    tags = ID3(path)
    tags.save(path, v2_version=4)
    backup = audio.snapshot(path, str(tmp_path / "b"))
    audio.write(path, {"title": "y"})
    assert ID3(path).version[1] == 3
    audio.restore(path, backup)
    assert ID3(path).version[1] == 4
    assert os.path.exists(backup)
