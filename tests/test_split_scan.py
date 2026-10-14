import json
import os

import pytest

from conftest import ffmpeg, needs_ffmpeg
from trackmend import audio, split
from trackmend.ops import Stop
from trackmend.store import Lock, Busy

# tone 5 s | silence 1.5 s | tone 7 s | silence 1.5 s | tone 4 s  = 19 s
LAYOUT = [("tone", 5), ("gap", 1.5), ("tone", 7), ("gap", 1.5), ("tone", 4)]


@pytest.fixture
def album_file(tmp_path, media):
    path = os.path.join(media, "Band - Full Album.mp3")
    inputs, labels = [], []
    for i, (kind, d) in enumerate(LAYOUT):
        src = f"sine=frequency={300 + 100 * i}:duration={d}" if kind == "tone" else f"anullsrc=r=44100:cl=mono:d={d}"
        inputs += ["-f", "lavfi", "-t", str(d), "-i", src]
        labels.append(f"[{i}:a]")
    graph = "".join(labels) + f"concat=n={len(LAYOUT)}:v=0:a=1[out]"
    ffmpeg(*inputs, "-filter_complex", graph, "-map", "[out]", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", path)
    audio.write(path, {"title": "Band - Full Album (Official Audio)", "artist": "BandVEVO"})
    return path


@needs_ffmpeg
def test_silences_are_found(album_file):
    sil = split.detect_silences(album_file)
    assert len(sil) == 2
    assert abs(sil[0][0] - 5) < 0.3 and abs(sil[1][1] - 15) < 0.3


def test_tracklist_with_start_times():
    segs = split.from_tracklist(["00:00 Intro", "0:06 Second Song", "1. 00:14 Third"], 19)
    assert [(s.start, s.end, s.title) for s in segs] == [(0, 6, "Intro"), (6, 14, "Second Song"), (14, 19, "Third")]


def test_tracklist_with_durations():
    segs = split.from_tracklist(["1. Intro (0:06)", "2. Second - 0:08", "3. Third 0:05"], 19)
    assert [round(s.end) for s in segs] == [6, 14, 19]
    assert [s.title for s in segs] == ["Intro", "Second", "Third"]


def test_tracklist_errors():
    with pytest.raises(Stop):
        split.from_tracklist(["only one"], 19)
    with pytest.raises(Stop):
        split.from_tracklist(["A 0:01", "B"], 19)
    with pytest.raises(Stop):
        split.from_tracklist(["A 9:00", "B 1:00"], 19)


def test_titles_only_use_the_longest_silences():
    sil = [(5.0, 6.5), (9.0, 9.2), (13.5, 15.0)]
    segs = split.from_tracklist(["A", "B", "C"], 19, silences=lambda: sil)
    assert [round(s.start, 1) for s in segs] == [0, 6.2, 14.7]


def test_deezer_track_list_is_snapped_to_silences():
    tracks = [{"title": "A", "duration": 7, "artist": {"name": "Band"}},
              {"title": "B", "duration": 8, "artist": {"name": "Band"}},
              {"title": "C", "duration": 4, "artist": {"name": "Band"}}]
    segs = split.from_deezer(tracks, 19.0, [(5.0, 6.5), (13.5, 15.0)])
    assert [round(s.start, 1) for s in segs] == [0, 6.2, 14.7]


def test_chapters():
    info = {"duration": 19, "chapters": [{"start_time": 0, "end_time": 6.5, "title": "01. Band - Intro"},
                                         {"start_time": 6.5, "end_time": 15, "title": "02. Band - Middle"},
                                         {"start_time": 15, "end_time": 19, "title": "03. Band - End"}]}
    segs = split.from_chapters(info, 19)
    split.name_segments(segs, "Band")
    assert [(s.artist, s.title) for s in segs] == [("Band", "Intro"), ("Band", "Middle"), ("Band", "End")]


@needs_ffmpeg
def test_split_end_to_end_and_undo(album_file, music, run_cli, tmp_path):
    pid = music.add_file(album_file, plays=4)
    tl = tmp_path / "tracks.txt"
    tl.write_text("00:00 Opening\n00:06 Middle Part\n00:15 The End\n")
    out_dir = str(tmp_path / "Music" / "split")
    code, out = run_cli("split", pid, "--tracklist", str(tl), "--album", "Full Album", "--year", "2001")
    assert code == 0 and "Nothing was changed" in out
    assert not os.path.exists(out_dir)
    code, out = run_cli("split", pid, "--tracklist", str(tl), "--album", "Full Album", "--year", "2001",
                        "--out", out_dir, "--apply", "--yes")
    assert code == 0, out
    new = sorted((t for t in music.all() if t.pid != pid), key=lambda t: t.track_number)
    assert [t.name for t in new] == ["Opening", "Middle Part", "The End"]
    assert pid not in {t.pid for t in music.all()}
    lengths = [audio.length(t.location) for t in new]
    assert [round(x) for x in lengths] == [6, 9, 4]
    tags = audio.read(new[1].location)
    assert (tags.artist, tags.album, tags.year, tags.track, tags.track_total) == ("Band", "Full Album", "2001", 2, 3)
    assert not os.path.exists(music.trash)
    code, out = run_cli("undo", "--apply", "--yes")
    assert code == 0, out
    left = music.all()
    assert len(left) == 1 and left[0].location == album_file and os.path.exists(album_file)


def test_find_track(music, make_audio):
    a = make_audio("a.mp3", title="Long Mix", artist="DJ")
    b = make_audio("b.mp3", title="Long Mix 2", artist="DJ")
    pa = music.add_file(a)
    music.add_file(b)
    tracks = music.tracks()
    assert split.find_track(tracks, pa).pid == pa
    with pytest.raises(Stop):
        split.find_track(tracks, "long mix")
    with pytest.raises(Stop):
        split.find_track(tracks, "nothing like this")


# -- scan --------------------------------------------------------------------------------

def test_scan_report(make_audio, music, run_cli, tmp_path):
    song = 31  # seconds: just long enough to count as a song
    music.add_file(make_audio("1.mp3", seconds=song, title="Song (Official Video)", artist="BandVEVO"))
    music.add_file(make_audio("2.mp3", seconds=song, title="Song", artist="Band", album="A", cover=True))
    music.add_file(make_audio("3.m4a", seconds=40, title="Wrong", artist="Band"), duration=2.0)
    music.add_file(make_audio("DJI_0042.mp3", seconds=song, title="DJI_0042"))
    music.add_file(make_audio("beep.mp3", seconds=3, title="Beep", artist="Band"))
    music.add_file(make_audio("4.mp3", seconds=21 * 60, title="Mix", artist="DJ"))
    music.add_file(make_audio("5.mp3", seconds=song, title="Track", artist="Band", genre="People & Blogs"))
    gone = make_audio("6.mp3", seconds=song, title="Gone")
    music.add_file(gone)
    os.remove(gone)
    js = tmp_path / "scan.json"
    code, out = run_cli("scan", "--json", str(js))
    assert code == 0
    found = json.loads(js.read_text())
    counts = {k: len(v) for k, v in found.items()}
    assert counts["noisy"] == 1 and counts["channel"] == 1 and counts["duplicates"] == 1
    assert counts["duration"] == 1 and counts["not_music"] == 2 and counts["long"] == 1
    assert counts["missing"] == 1 and counts["youtube_genre"] == 1
    assert "Nothing was changed" in out
    assert not [c for c in music.calls if c[0] in ("refresh", "set", "delete", "add")]


def test_scan_details(make_audio, music, run_cli):
    music.add_file(make_audio("1.mp3", seconds=31, title="Song (Official Video)", artist="Band"))
    code, out = run_cli("scan", "--details")
    assert "Song (Official Video)" in out


# -- lock --------------------------------------------------------------------------------

def test_second_command_is_refused_while_one_runs(home, run_cli):
    with Lock(os.path.join(home, "lock"), "tags"):
        code, out = run_cli("dedup")
        assert code == 75 and "already working" in out
        with pytest.raises(Busy):
            with Lock(os.path.join(home, "lock"), "lyrics"):
                pass
    code, out = run_cli("dedup")
    assert code == 0


def test_scan_does_not_need_the_lock(home, run_cli):
    with Lock(os.path.join(home, "lock"), "tags"):
        code, out = run_cli("scan")
        assert code == 0
