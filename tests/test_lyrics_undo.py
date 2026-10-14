import json
import os

from trackmend import audio
from test_matching import dz_track, hit
from test_tags import COVER


def lrclib_routes(web, by_title):
    def search(url):
        for title, answer in by_title.items():
            if f"track_name={title}" in url or f"q=Band+{title}" in url:
                return answer
        return []
    web.routes["lrclib.net/api/search"] = search
    web.routes["lrclib.net/api/get"] = (404, b"")


def test_lyrics_dry_run_and_apply(make_audio, run_cli, music, web, home):
    pids = []
    for i, d in enumerate((200, 260, 200)):
        p = make_audio(f"s{i}.mp3", title=f"Song{i}", artist="Band")
        pids.append(music.add_file(p, duration=float(d)))
    lrclib_routes(web, {
        "Song0": [hit(1, "Band", "Song0", 201, plain="zero")],
        "Song1": [hit(2, "Band", "Song1", 200, plain="one, video version")],  # 60 s longer clip
        "Song2": [hit(3, "Other", "Song2", 230, plain="wrong song")],         # not the same artist, 30 s off
    })
    code, out = run_cli("lyrics")
    assert "2 lyrics found" in out and "Nothing was changed" in out
    assert music.lyrics(pids[0]) == ""
    code, out = run_cli("lyrics", "--apply")
    assert code == 0, out
    assert music.lyrics(pids[0]) == "zero"
    assert music.lyrics(pids[1]) == "one, video version"
    assert music.lyrics(pids[2]) == ""
    code, out = run_cli("lyrics", "--apply")
    assert "0 tracks without lyrics" in out


def test_lyrics_are_written_in_small_batches(make_audio, run_cli, music, web):
    for i in range(25):
        p = make_audio(f"b{i}.mp3", title=f"Track{i}", artist="Band")
        music.add_file(p, duration=200.0)
    web.routes["lrclib.net/api/search"] = lambda url: [hit(1, "Band", url.split("track_name=")[1].split("&")[0],
                                                            200, plain="words")] if "track_name=" in url else []
    run_cli("lyrics", "--apply")
    sets = [c for c in music.calls if c[0] == "set"]
    assert len(sets) == 3 and max(len(c[1]) for c in sets) == 10


def test_undo_tags_restores_files_and_empties_new_fields_in_music(make_audio, run_cli, music, web, home):
    path = make_audio("h.mp3", title="HUMBLE. (Official Video)", artist="KendrickLamarVEVO")
    pid = music.add_file(path, duration=180.0)
    original = audio.read(path).as_dict()
    web.routes.update({
        "search?": {"data": [dz_track(1, "HUMBLE.", "Kendrick Lamar", 177, album="DAMN.", album_id=55)]},
        "track/1": {"release_date": "2017-01-01", "track_position": 8},
        "album/55": {"title": "DAMN.", "artist": {"name": "Kendrick Lamar"}, "nb_tracks": 14},
        "cdn/1.jpg": (200, COVER),
    })
    run_cli("tags", "--apply")
    assert music.get(pid).album == "DAMN." and music.shown_artwork(pid) == COVER
    code, out = run_cli("undo")
    assert "restore tags" in out and "Nothing was changed" in out
    code, out = run_cli("undo", "--apply")
    assert code == 0, out
    assert audio.read(path).as_dict() == original
    assert not audio.read(path).has_cover
    assert music.shown_artwork(pid) is None      # Music no longer shows the new artwork either
    t = music.get(pid)
    # Music's refresh would have kept "DAMN."; undo empties it explicitly.
    assert (t.name, t.artist, t.album, t.year) == ("HUMBLE. (Official Video)", "KendrickLamarVEVO", "", 0)
    code, out = run_cli("undo", "--apply")
    assert "Nothing to undo" in out
    # the track can be processed again after an undo
    code, out = run_cli("tags")
    assert "1 tracks to look up" in out


def test_undo_lyrics(make_audio, run_cli, music, web):
    p = make_audio("s.mp3", title="Song0", artist="Band")
    pid = music.add_file(p, duration=200.0)
    lrclib_routes(web, {"Song0": [hit(1, "Band", "Song0", 200, plain="zero")]})
    run_cli("lyrics", "--apply")
    assert music.lyrics(pid) == "zero"
    run_cli("undo", "--apply")
    assert music.lyrics(pid) == ""


def test_undo_list_and_unknown_run(run_cli):
    code, out = run_cli("undo", "--list")
    assert "No runs yet" in out
    code, out = run_cli("undo", "nope")
    assert code == 1


def test_journal_survives_a_crash_mid_write(make_audio, music, home, monkeypatch, run_cli, web):
    from trackmend import audio as audio_mod
    p = make_audio("c.mp3", title="Band - Song (Official Video)")
    music.add_file(p, duration=200.0)
    web.routes["search?"] = {"data": []}
    real_write = audio_mod.write

    def crash(*a, **k):
        real_write(*a, **k)
        raise SystemExit("power cut")

    monkeypatch.setattr(audio_mod, "write", crash)
    try:
        run_cli("tags", "--apply")
    except SystemExit:
        pass
    monkeypatch.setattr(audio_mod, "write", real_write)
    run_dir = os.path.join(home, "runs", os.listdir(os.path.join(home, "runs"))[0])
    lines = [json.loads(x) for x in open(os.path.join(run_dir, "journal.jsonl"))]
    assert lines[0]["status"] == "pending" and any("backup" in x for x in lines)
    assert audio.read(p).title == "Song"
    run_cli("undo", "--apply")
    assert audio.read(p).title == "Band - Song (Official Video)"


# -- LRCLIB busy, and the order tags -> lyrics ------------------------------------------

def test_busy_lrclib_means_to_redo_not_not_found(make_audio, run_cli, music, web, home):
    p = make_audio("busy.mp3", title="Song", artist="Band")
    pid = music.add_file(p, duration=200.0)
    web.routes["lrclib.net"] = (503, b"")
    code, out = run_cli("lyrics", "--apply")
    assert code == 0, out
    assert "trying them once more" in out and "to redo" in out and "0 not found" in out
    from trackmend.store import Home
    progress = Home(home).progress("lyrics")
    assert progress.entries()[pid][0] == "retry"
    progress.close()
    # next run: LRCLIB answers again, the track is looked up without --retry
    web.routes["lrclib.net"] = (404, b"")
    lrclib_routes(web, {"Song": [hit(1, "Band", "Song", 200, plain="words")]})
    code, out = run_cli("lyrics", "--apply")
    assert "1 tracks without lyrics to look up" in out
    assert music.lyrics(pid) == "words"


def _bossa(make_audio, music, web):
    p = make_audio("bossa.mp3", title="Track 03", artist="Unknown Artist")
    pid = music.add_file(p, duration=200.0)
    lrclib_routes(web, {"Desafinado": [hit(7, "João Gilberto", "Desafinado", 201, plain="desafinado")]})
    web.routes["lrclib.net/api/search?q="] = []
    return p, pid


def test_lyrics_are_looked_up_again_when_tags_rename_a_track(make_audio, run_cli, music, web):
    p, pid = _bossa(make_audio, music, web)
    code, out = run_cli("lyrics", "--apply")                 # with the raw names: nothing
    assert "0 lyrics added, 1 not found" in out
    web.routes.update({
        "api.deezer.com/search?": {"data": [dz_track(9, "Desafinado", "João Gilberto", 201, album="Bossa",
                                                     album_id=90)]},
        "track/9": {"title": "Desafinado"}, "album/90": {"title": "Bossa"},
    })
    music.get(pid).name, music.get(pid).artist = "João Gilberto - Desafinado", ""
    code, out = run_cli("tags", "--no-artwork")              # dry run says it will happen
    assert "Lyrics will be looked up again for 1" in out
    assert music.lyrics(pid) == ""
    code, out = run_cli("tags", "--apply", "--no-artwork")
    assert code == 0, out
    assert "looking again" in out and "1 lyrics added" in out
    assert music.lyrics(pid) == "desafinado"
    code, out = run_cli("undo", "--list")
    assert out.count("lyrics") >= 2                          # its own run, undoable on its own


def test_lyrics_command_looks_again_when_names_changed_elsewhere(make_audio, run_cli, music, web):
    p, pid = _bossa(make_audio, music, web)
    run_cli("lyrics", "--apply")
    code, out = run_cli("lyrics")
    assert "0 tracks without lyrics" in out                  # settled while nothing changed
    music.get(pid).name, music.get(pid).artist = "Desafinado", "João Gilberto"   # fixed by hand in Music
    code, out = run_cli("lyrics", "--apply")
    assert "1 tracks without lyrics" in out and music.lyrics(pid) == "desafinado"


def test_tags_do_not_search_lyrics_never_looked_up(make_audio, run_cli, music, web):
    p, pid = _bossa(make_audio, music, web)
    music.get(pid).name, music.get(pid).artist = "João Gilberto - Desafinado", ""
    web.routes.update({
        "api.deezer.com/search?": {"data": [dz_track(9, "Desafinado", "João Gilberto", 201)]},
        "track/9": {"title": "Desafinado"},
    })
    code, out = run_cli("tags", "--apply", "--no-artwork")
    assert "Lyrics" not in out and music.lyrics(pid) == ""   # lyrics come last, when asked


def test_lyrics_say_to_fix_names_first(make_audio, run_cli, music, web):
    p = make_audio("raw.mp3", title="Band - Song (Official Video)", artist="BandVEVO")
    music.add_file(p, duration=200.0)
    code, out = run_cli("lyrics")
    assert "run `trackmend tags --apply` first" in out
