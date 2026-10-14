import hashlib
import os

import pytest

from conftest import png_bytes
from fakes import FakeHttp, FakeMusic
from trackmend import audio
from trackmend.music import Track
from test_matching import dz_track

# A JPEG header saying 1000x1000, padded to look like a real download.
COVER = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
         b"\xff\xc0\x00\x11\x08\x03\xe8\x03\xe8\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01" + b"\x00" * 4000)


def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


@pytest.fixture
def humble(make_audio, music, web):
    path = make_audio("HUMBLE.mp3", seconds=3, title="HUMBLE. (Official Video)", artist="KendrickLamarVEVO")
    pid = music.add_file(path, duration=180.0)
    web.routes.update({
        "search?": {"data": [dz_track(1, "HUMBLE.", "Kendrick Lamar", 177, album="DAMN.", album_id=55)]},
        "track/1": {"title": "HUMBLE.", "release_date": "2017-03-30", "track_position": 8, "disk_number": 1},
        "album/55": {"title": "DAMN.", "artist": {"name": "Kendrick Lamar"}, "nb_tracks": 14,
                     "genres": {"data": [{"name": "Rap/Hip Hop"}]}},
        "cdn/1.jpg": (200, COVER),
    })
    return pid, path


def test_dry_run_changes_nothing(humble, run_cli, home, music):
    pid, path = humble
    before = digest(path)
    code, out = run_cli("tags")
    assert code == 0
    assert "Kendrick Lamar - HUMBLE." in out and "DAMN." in out
    assert "Nothing was changed" in out
    assert digest(path) == before
    assert not os.path.isdir(os.path.join(home, "runs"))
    assert not [c for c in music.calls if c[0] in ("refresh", "set", "delete", "add")]


def test_apply_writes_backs_up_and_refreshes(humble, run_cli, home, music):
    pid, path = humble
    code, out = run_cli("tags", "--apply")
    assert code == 0, out
    t = audio.read(path)
    assert (t.title, t.artist, t.album, t.year, t.track, t.track_total) == ("HUMBLE.", "Kendrick Lamar", "DAMN.",
                                                                            "2017", 8, 14)
    assert audio.cover_bytes(path) == COVER
    assert ("refresh", [pid]) in music.calls
    assert music.get(pid).name == "HUMBLE." and music.get(pid).album == "DAMN."
    # refresh alone leaves Music showing its cached image: the artwork goes through Music
    assert music.shown_artwork(pid) == COVER
    assert music.calls.index(("set_artwork", pid)) > music.calls.index(("refresh", [pid]))
    runs = os.listdir(os.path.join(home, "runs"))
    backups = os.listdir(os.path.join(home, "runs", runs[0], "backup"))
    assert any(b.endswith(".id3") for b in backups)


def test_second_run_skips_what_is_done(humble, run_cli, web):
    run_cli("tags", "--apply")
    n = web.count("search?")
    code, out = run_cli("tags", "--apply", "--all")
    assert "0 tracks to look up" in out
    assert web.count("search?") == n


def test_interrupted_run_resumes_without_losing_writes(make_audio, run_cli, music, web):
    paths = []
    for i in range(5):
        p = make_audio(f"t{i}.mp3", title=f"Song {i} (Official Video)", artist="Band")
        music.add_file(p, duration=200.0)
        paths.append(p)
    calls = {"n": 0}

    def search(url):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt
        return {"data": []}

    web.routes["search?"] = search
    code, out = run_cli("tags", "--apply", "--no-artwork")
    assert "Stopped" in out
    written = [p for p in paths if audio.read(p).title.startswith("Song") and "Official" not in audio.read(p).title]
    assert len(written) >= 1
    # every written file was also refreshed in Music before stopping
    refreshed = {pid for c in music.calls if c[0] == "refresh" for pid in c[1]}
    assert {pid for pid, t in ((t.pid, t) for t in music.all()) if "Official" not in t.name} <= refreshed

    web.routes["search?"] = {"data": []}
    code, out = run_cli("tags", "--apply", "--no-artwork")
    assert all("Official" not in audio.read(p).title for p in paths)
    assert f"{5 - len(written)} tracks to look up" in out


def test_location_is_read_again_before_writing(make_audio, run_cli, media, web, home):
    music = FakeMusic(media, organize=True)
    a = make_audio("a.mp3", title="Band - First (Official Video)")
    b = make_audio("b.mp3", title="Band - Second (Official Video)")
    pa = music.add_file(a, duration=200.0)
    pb = music.add_file(b, duration=200.0)
    stale = {t.pid: t.location for t in music.tracks()}
    # Music organizes the folder after an earlier refresh: both files move.
    music.refresh([pa, pb])
    assert music.get(pa).location != stale[pa]
    web.routes["search?"] = {"data": []}
    code, out = run_cli("tags", "--apply", library=music)
    assert code == 0, out
    assert not os.path.exists(stale[pa]) and not os.path.exists(stale[pb])
    assert audio.read(music.get(pa).location).title == "First"
    assert audio.read(music.get(pb).location).title == "Second"
    assert music.get(pa).artist == "Band"


def test_live_version_keeps_its_title_and_gets_no_studio_artwork(make_audio, run_cli, music, web):
    p = make_audio("live.mp3", title="Phoebe Bridgers - Motion Sickness (Live at Electric Lady) | Official Video")
    music.add_file(p, duration=231.0)
    web.routes.update({
        "search?": {"data": [dz_track(3, "Motion Sickness", "Phoebe Bridgers", 230, album="Stranger in the Alps")]},
        "cdn/3.jpg": (200, COVER),
    })
    code, out = run_cli("tags", "--apply")
    t = audio.read(p)
    assert t.title == "Motion Sickness (Live at Electric Lady)"
    assert t.artist == "Phoebe Bridgers"
    assert t.album == "" and not t.has_cover
    assert "other version" in out


def test_artist_prefix_is_removed_even_when_artist_is_set(make_audio, run_cli, music, web):
    p = make_audio("x.mp3", title="Nujabes — Aruarian Dance", artist="Nujabes", album="Samurai Champloo")
    music.add_file(p, duration=200.0)
    web.routes["search?"] = {"data": []}
    run_cli("tags", "--apply")
    assert audio.read(p).title == "Aruarian Dance"


def test_complete_tracks_are_left_alone(make_audio, run_cli, music, web):
    p = make_audio("ok.mp3", title="Song", artist="Band", album="Album", cover=True)
    music.add_file(p, duration=200.0)
    code, out = run_cli("tags")
    assert "0 tracks to look up" in out
    assert not web.calls


def test_csv_report(humble, run_cli, tmp_path):
    report = tmp_path / "plan.csv"
    run_cli("tags", "--report", str(report))
    rows = report.read_text().splitlines()
    assert rows[0].startswith("persistent_id,old_artist,old_title")
    assert "DAMN." in rows[1]


def test_existing_artwork_is_kept_unless_asked(make_audio, run_cli, music, web):
    p = make_audio("c.mp3", title="HUMBLE.", artist="Kendrick Lamar", album="DAMN.", cover=True)
    pid = music.add_file(p, duration=177.0)
    web.routes.update({
        "search?": {"data": [dz_track(1, "HUMBLE.", "Kendrick Lamar", 177, album="DAMN.", album_id=55)]},
        "track/1": {"title": "HUMBLE."}, "album/55": {"title": "DAMN."}, "cdn/1.jpg": (200, COVER),
    })
    run_cli("tags", "--apply", "--all")
    assert audio.cover_bytes(p) == png_bytes()
    assert ("set_artwork", pid) not in music.calls
    run_cli("tags", "--apply", "--all", "--redo", "--replace-artwork")
    assert audio.cover_bytes(p) == COVER
    assert music.shown_artwork(pid) == COVER


def test_playlist_limits_the_scope(make_audio, run_cli, music, web):
    a = make_audio("a.mp3", title="A (Official Video)", artist="Band")
    b = make_audio("b.mp3", title="B (Official Video)", artist="Band")
    music.add_file(a, playlist="test", duration=200.0)
    music.add_file(b, duration=200.0)
    web.routes["search?"] = {"data": []}
    run_cli("tags", "--apply", "--playlist", "test")
    assert audio.read(a).title == "A" and audio.read(b).title == "B (Official Video)"
    code, out = run_cli("tags", "--playlist", "missing")
    assert code == 1


def test_unused_fakehttp_default():
    assert FakeHttp()("https://x", {})[0] == 404


def test_file_replaced_after_the_export_is_found_by_persistent_id(make_audio, run_cli, music, web, media):
    # Another script swaps the clip for the official version (.mp3 -> .m4a) during the run.
    old = make_audio("clip.mp3", title="HUMBLE. (Official Video)", artist="KendrickLamarVEVO")
    pid = music.add_file(old, duration=180.0)
    export = [t.to_dict() for t in music.tracks()]                # taken before the swap
    new = make_audio("official.m4a", title="HUMBLE. (Official Video)", artist="KendrickLamarVEVO")
    music.tracks = lambda playlist=None: [Track.from_dict(d) for d in export]   # still says clip.mp3
    music.get(pid).location = new
    os.remove(old)
    music._inode[pid] = (os.stat(new).st_dev, os.stat(new).st_ino)
    web.routes.update({
        "search?": {"data": [dz_track(1, "HUMBLE.", "Kendrick Lamar", 177, album="DAMN.", album_id=55)]},
        "track/1": {"title": "HUMBLE."}, "album/55": {"title": "DAMN."}, "cdn/1.jpg": (200, COVER),
    })
    code, out = run_cli("tags", "--apply", "--no-artwork")
    assert code == 0, out
    assert audio.read(new).album == "DAMN." and music.get(pid).album == "DAMN."
