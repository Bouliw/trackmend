import os

from fakes import FakeMusic
from trackmend.dedup import find_duplicates


def lib(make_audio, music):
    a = make_audio("a.mp3", title="Song", artist="Band")
    b = make_audio("b.mp3", title="Band - Song (Official Video)", artist="BandVEVO")
    c = make_audio("c.mp3", title="Song (Live)", artist="Band")
    d = make_audio("d.mp3", title="Song", artist="Band", folder=os.path.join(os.path.dirname(music.media), "Elsewhere"))
    pa = music.add_file(a, duration=200.0, plays=3)
    pb = music.add_file(b, duration=202.0, plays=10, lyrics_len=0)
    pc = music.add_file(c, duration=201.0)
    pd = music.add_file(d, duration=260.0)
    return pa, pb, pc, pd


def test_groups_keep_the_most_played_and_spare_other_versions(make_audio, music):
    pa, pb, pc, pd = lib(make_audio, music)
    d = find_duplicates(music.tracks())
    assert len(d.groups) == 1
    g = d.groups[0]
    assert g.keep.pid == pb and [t.pid for t in g.remove] == [pa]  # live take and 1-minute-longer cut stay


def test_favorite_wins(make_audio, music):
    a = make_audio("a.mp3", title="Song", artist="Band")
    b = make_audio("b.mp3", title="Song", artist="Band")
    music.add_file(a, duration=200.0, plays=50)
    pb = music.add_file(b, duration=200.0, favorited=True)
    assert find_duplicates(music.tracks()).groups[0].keep.pid == pb


def test_maybe_list_for_tracks_without_artist(make_audio, music):
    a = make_audio("a.mp3", title="Rare Song", artist="Band")
    b = make_audio("b.mp3", title="Rare Song")
    music.add_file(a, duration=200.0)
    music.add_file(b, duration=201.0)
    d = find_duplicates(music.tracks())
    assert not d.groups and len(d.maybe) == 1


def test_nothing_removed_without_confirmation(make_audio, music, run_cli):
    pa, *_ = lib(make_audio, music)
    code, out = run_cli("dedup", "--apply")
    assert "not confirmed" in out
    assert pa in {t.pid for t in music.all()}


def test_removed_copy_is_kept_and_undo_puts_it_back(make_audio, music, run_cli, home):
    pa, pb, pc, pd = lib(make_audio, music)
    path_a = music.get(pa).location
    code, out = run_cli("dedup", "--apply", "--yes")
    assert code == 0, out
    assert pa not in {t.pid for t in music.all()}
    assert not os.path.exists(music.trash)  # nothing went to the Trash
    kept = [os.path.join(dp, f) for dp, _, fs in os.walk(os.path.join(home, "runs")) for f in fs if f.endswith("a.mp3")]
    assert len(kept) == 1 and not os.path.exists(path_a)
    code, out = run_cli("undo", "--apply")
    assert code == 0, out
    back = [t for t in music.all() if t.location == path_a]
    assert len(back) == 1 and back[0].plays == 3


def test_when_music_follows_moved_files_a_copy_is_kept(make_audio, media, run_cli, home):
    music = FakeMusic(media, follow_moves=True)
    a = make_audio("a.mp3", title="Song", artist="Band")
    b = make_audio("b.mp3", title="Song", artist="Band")
    music.add_file(a, duration=200.0)
    music.add_file(b, duration=200.0, plays=5)
    code, out = run_cli("dedup", "--apply", "--yes", library=music)
    assert code == 0, out
    assert os.path.exists(os.path.join(music.trash, "a.mp3"))
    kept = [f for dp, _, fs in os.walk(os.path.join(home, "runs")) for f in fs if f.endswith("a.mp3")]
    assert kept


def test_reimport_fixes_wrong_durations(make_audio, music, run_cli):
    p = make_audio("cut.m4a", seconds=8, title="Song", artist="Band")
    pid = music.add_file(p, duration=2.0, plays=7, favorited=True)
    ok = make_audio("ok.mp3", seconds=3, title="Other", artist="Band")
    music.add_file(ok)
    code, out = run_cli("reimport")
    assert "1 tracks show a wrong duration" in out and "Nothing was changed" in out
    code, out = run_cli("reimport", "--apply", "--yes")
    assert code == 0, out
    assert pid not in {t.pid for t in music.all()}
    new = [t for t in music.all() if t.location == p]
    assert len(new) == 1 and 7 < new[0].duration < 9 and new[0].plays == 7 and new[0].favorited
