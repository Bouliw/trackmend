import unicodedata

import pytest

from trackmend import text


@pytest.mark.parametrize("name, artist, want_artist, want_title", [
    ("HUMBLE. (Official Video)", "KendrickLamarVEVO", "Kendrick Lamar", "HUMBLE."),
    ("Kendrick Lamar - HUMBLE.", "KendrickLamarVEVO", "Kendrick Lamar", "HUMBLE."),
    ("Kendrick Lamar — HUMBLE. [Official Music Video]", "Kendrick Lamar", "Kendrick Lamar", "HUMBLE."),
    ("Kendrick Lamar – HUMBLE.", "Kendrick Lamar", "Kendrick Lamar", "HUMBLE."),
    ("Kendrick Lamar–HUMBLE.", "", "Kendrick Lamar", "HUMBLE."),
    ("Kendrick Lamar | HUMBLE.", "", "Kendrick Lamar", "HUMBLE."),
    ("Kendrick Lamar ｜ HUMBLE.", "", "Kendrick Lamar", "HUMBLE."),
    ("Artist /// Title", "", "Artist", "Title"),
    ("Jay-Z - 99 Problems", "", "Jay-Z", "99 Problems"),
    ("Get Lucky ft. Pharrell", "Daft Punk - Topic", "Daft Punk", "Get Lucky (feat. Pharrell)"),
    ("Daft Punk - Get Lucky feat Pharrell Williams (Official Audio)", "", "Daft Punk",
     "Get Lucky (feat. Pharrell Williams)"),
    ("Lana Del Rey - Video Games (Lyrics)", "", "Lana Del Rey", "Video Games"),
    ("Oasis - Live Forever (Remastered 2014)", "Oasis", "Oasis", "Live Forever"),
    ("Bohemian Rhapsody - Remastered 2011", "Queen", "Queen", "Bohemian Rhapsody"),
    ("Artist - Song - Official Video", "", "Artist", "Song"),
    ("Song Official Music Video", "Band", "Band", "Song"),
    ("YOASOBI「夜に駆ける」Official Music Video", "", "YOASOBI",
     "夜に駆ける"),
    ("Nujabes - Aruarian Dance \U0001f3a7\U0001f525", "", "Nujabes", "Aruarian Dance"),
])
def test_parse_artist_and_title(name, artist, want_artist, want_title):
    p = text.parse(name, artist)
    assert (p.artist, p.title) == (want_artist, want_title)


@pytest.mark.parametrize("name, artist", [
    ("Video", "India.Arie"),
    ("Home Video", "Lucy Dacus"),
    ("Live 1975–1976", "Bruce Springsteen"),
    ("Title - Pt. 2", "Artist"),
    ("Drôle De Décision", "Artiste"),
])
def test_parse_leaves_real_titles_alone(name, artist):
    p = text.parse(name, artist)
    assert p.title == name and p.artist == artist


def test_ambiguous_artist_field_keeps_both_readings():
    p = text.parse("Juice WRLD - All Girls Are The Same", "Some Producer")
    assert p.artist == "Some Producer"
    assert ("Juice WRLD", "All Girls Are The Same") in p.readings
    assert ("All Girls Are The Same", "Juice WRLD") in p.readings


@pytest.mark.parametrize("name, artist, want_artist, want_title", [
    ("Juice WRLD - All Girls Are The Same", "Lyrical Lemonade", "Juice WRLD", "All Girls Are The Same"),
    ("Artist - Song | A COLORS SHOW", "COLORS", "Artist", "Song (A COLORS SHOW)"),
    ("Song Title", "COLORS", "", "Song Title"),
    ("Mac Miller - Good News", "Spinnin Records", "Mac Miller", "Good News"),
    ("Phoebe Bridgers - Kyoto", "NPR Music", "Phoebe Bridgers", "Kyoto"),
    ("Daft Punk - Digital Love", "alfiedits", "Daft Punk", "Digital Love"),
    ("More Than This", "Roxy Music", "Roxy Music", "More Than This"),
    ("Song - Artist Name", "Artist Name", "Artist Name", "Song"),
])
def test_channel_names_in_the_artist_field(name, artist, want_artist, want_title):
    p = text.parse(name, artist)
    assert (p.artist, p.title) == (want_artist, want_title)


def test_session_channels_mark_live_versions():
    assert "live" in text.parse("Artist - Song", "COLORS").qualifiers
    assert "live" in text.parse("Artist - Song", "NPR Music").qualifiers
    assert not text.parse("Artist - Song", "Majestic Casual").qualifiers


@pytest.mark.parametrize("name, artist, want", [
    ("Discovery - 03. Digital Love", "Daft Punk", ("Daft Punk", "Digital Love", "Discovery", "", 3)),
    ("Daft Punk - Discovery - 03. Digital Love", "", ("Daft Punk", "Digital Love", "Discovery", "", 3)),
    ("[01] Feel The Need (Disco 1979)", "Band", ("Band", "Feel The Need", "Disco", "1979", 1)),
    ("01. Intro", "Band", ("Band", "Intro", "", "", 1)),
    ("99 Problems", "Jay-Z", ("Jay-Z", "99 Problems", "", "", 0)),
    ("1999", "Prince", ("Prince", "1999", "", "", 0)),
    ("Song (Live 1979)", "Band", ("Band", "Song (Live 1979)", "", "", 0)),
])
def test_album_and_track_number_in_the_name(name, artist, want):
    p = text.parse(name, artist)
    assert (p.artist, p.title, p.album_hint, p.year_hint, p.track_hint) == want


@pytest.mark.parametrize("name, quals, title", [
    ("Bon Iver – Skinny Love (Live) [HD]", {"live"}, "Skinny Love (Live)"),
    ("Song (Official Live Video)", {"live"}, "Song (Live)"),
    ("Song Name | Garage Session", {"live"}, "Song Name (Garage Session)"),
    ("Artist - Title (Sped Up)", {"altered"}, "Title (Sped Up)"),
    ("Artist - Title (Acoustic Version)", {"acoustic"}, "Title (Acoustic Version)"),
    ("Artist - Title (Radio Edit)", {"edit"}, "Title (Radio Edit)"),
    ("Artist - Title (Demo)", {"demo"}, "Title (Demo)"),
    ("Artist - Title [Cover]", {"cover"}, "Title [Cover]"),
    ("Artist - Title (Remix)", {"remix"}, "Title (Remix)"),
])
def test_version_words_are_kept(name, quals, title):
    p = text.parse(name, "")
    assert set(p.qualifiers) == quals
    assert p.title == title


def test_brackets_with_real_words_are_kept():
    p = text.parse("Casio (Recorded at Deezer HQ, Paris)", "Jungle")
    assert p.title == "Casio (Recorded at Deezer HQ, Paris)" and "live" in p.qualifiers
    assert text.parse("Song (Color Coded Lyrics Eng/Rom/Han)", "Band").title == "Song"
    assert text.parse("Song (Han Solo)", "Band").title == "Song (Han Solo)"


def test_featuring_goes_before_version_brackets():
    p = text.parse("She Wants Me Dead (feat. The High) [CAZZETTE vs. AronChupa]", "Cazzette")
    assert p.title == "She Wants Me Dead (feat. The High) [CAZZETTE vs. AronChupa]"
    assert text.parse("Song [Remix] ft The Guest", "Band").title == "Song (feat. The Guest) [Remix]"


@pytest.mark.parametrize("name", ["Live Forever", "Live and Let Die", "Under Cover of Darkness",
                                  "Mix Tape", "Title (Original Mix)", "Title (Album Version)"])
def test_title_words_are_not_versions(name):
    assert not text.parse(name, "Band").qualifiers


def test_unicode_forms_compare_equal():
    nfc = "Décision Été"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    assert text.norm(nfc) == text.norm(nfd) == "decision ete"
    assert text.norm("Don’t Stop Me Now") == text.norm("Don't Stop Me Now") == text.norm("Dont Stop Me Now")
    assert text.norm("ＡＢＣ") == "abc"  # full-width letters


def test_japanese_voicing_marks_are_kept():
    a, b = "かける", "がける"
    assert text.norm(a) != text.norm(b)
    assert text.norm(unicodedata.normalize("NFD", b)) == text.norm(b)


def test_emojis_do_not_break_comparisons():
    assert text.norm("Sunflower \U0001f33b") == text.norm("Sunflower")
    assert text.core_title("\U0001f525 Fire \U0001f525") == "fire"


@pytest.mark.parametrize("raw, want", [
    ("KendrickLamarVEVO", "Kendrick Lamar"),
    ("LilNasXVEVO", "Lil Nas X"),
    ("Daft Punk - Topic", "Daft Punk"),
    ("Some Band Official", "Some Band"),
    ("BTS", "BTS"),
])
def test_clean_artist(raw, want):
    assert text.clean_artist(raw) == want


@pytest.mark.parametrize("raw, want", [
    ("Drake feat. Rihanna", "Drake"), ("Drake feat Rihanna", "Drake"), ("A x B", "A"),
    ("Simon & Garfunkel", "Simon"), ("Lil Nas X", "Lil Nas X"), ("AC/DC", "AC/DC"),
])
def test_primary_artist(raw, want):
    assert text.primary_artist(raw) == want


def test_same_artist():
    assert text.same_artist("Kendrick Lamar", "KendrickLamar")
    assert text.same_artist("Kendrick Lamar", "Kendrick Lamar, SZA")
    assert text.same_artist("KoЯn", "Korn")
    assert not text.same_artist("Title", "Artist")


def test_short_artist_names_match_whole_words_only():
    assert text.norm("((( O )))") == "o"
    assert not text.same_artist("((( O )))", "Foo Fighters")
    assert not text.same_artist("((( O )))", "Oasis")
    assert text.same_artist("((( O )))", "O")


@pytest.mark.parametrize("local, catalogue, same", [
    ("Inachevs", "Inachev\u00e9s", True),
    ("Youre Beautiful", "You\u2019re Beautiful", True),
    ("Tout Lcher", "Tout L\u00e2cher", True),
    ("Tout Lacher", "Tout L\u00e2cher", True),
    ("Song, Pt. II", "Song Pt. 2", True),
    ("Song Pt. II", "Song Pt. III", False),
    ("Song (Part 1)", "Song", False),
    ("Plastic Love", "Plastic Love (\u30d7\u30e9\u30b9\u30c6\u30a3\u30c3\u30af\u30fb\u30e9\u30f4)", True),
    ("Other", "Song", False),
])
def test_same_title(local, catalogue, same):
    assert text.same_title(local, catalogue) is same


def test_katakana_reading_is_removed():
    assert text.strip_gloss("Plastic Love (\u30d7\u30e9\u30b9\u30c6\u30a3\u30c3\u30af\u30fb\u30e9\u30f4)") == "Plastic Love"
    assert text.strip_gloss("Stay With Me - \u30b9\u30c6\u30a4\u30fb\u30a6\u30a3\u30ba\u30fb\u30df\u30fc") == "Stay With Me"
    kanji = "\u771f\u591c\u4e2d\u306e\u30c9\u30a2"
    assert text.strip_gloss(kanji) == kanji  # a Japanese title stays


def test_needs_cleanup():
    assert text.needs_cleanup("Song (Official Video)", "Band")
    assert text.needs_cleanup("Band - Song", "Band")
    assert text.needs_cleanup("Song", "BandVEVO")
    assert not text.needs_cleanup("Song", "Band")


def test_safe_filename():
    assert text.safe_filename('AC/DC: "Back" <In> Black?') == "AC-DC- -Back- -In- Black"
    assert text.safe_filename("") == "untitled"
