"""Cleaning and comparing track names.

Names coming from YouTube rips and old MP3 collections look like
``KendrickLamarVEVO`` / ``Kendrick Lamar - HUMBLE. (Official Video)``. This module
turns them into an artist, a clean title and the "version" words that must be kept
(live, acoustic, remix...), and gives comparison keys that survive emojis, Japanese
text, typographic apostrophes and NFC/NFD differences.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import FrozenSet, List, Optional, Set, Tuple

# --------------------------------------------------------------------------------------
# Unicode folding
# --------------------------------------------------------------------------------------

_INVISIBLE = str.maketrans({
    "\u00a0": " ", "\u2007": " ", "\u2009": " ", "\u202f": " ", "\u3000": " ",
    "\u200b": None, "\u200c": None, "\u200d": None, "\u2060": None, "\ufeff": None,
    "\ufe0f": None, "\ufe0e": None,
})

_QUOTES = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u02bc": "'", "\u02bb": "'",
    "\u2032": "'", "\u00b4": "'", "`": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
})


def tidy(s: Optional[str]) -> str:
    """Display-safe cleanup: NFC, no zero-width characters, single spaces."""
    s = unicodedata.normalize("NFC", s or "").translate(_INVISIBLE)
    return " ".join(s.split())


# Cyrillic and Greek letters used to style Latin names (KoЯn, NIИ, ΛRT).
_LOOKALIKE = str.maketrans({
    "\u044f": "r", "\u042f": "R", "\u0438": "n", "\u0418": "N", "\u0430": "a", "\u0410": "A",
    "\u0435": "e", "\u0415": "E", "\u043e": "o", "\u041e": "O", "\u0441": "c", "\u0421": "C",
    "\u0440": "p", "\u0420": "P", "\u0445": "x", "\u0425": "X", "\u0443": "y", "\u0423": "Y",
    "\u043a": "k", "\u041a": "K", "\u043c": "m", "\u041c": "M", "\u0442": "t", "\u0422": "T",
    "\u0432": "b", "\u0412": "B", "\u043d": "h", "\u041d": "H", "\u0455": "s", "\u0456": "i",
    "\u0458": "j", "\u03bb": "a", "\u039b": "A", "\u03a3": "E", "\u0394": "A", "\u03bf": "o",
    "\u039f": "O", "\u03b1": "a", "\u0391": "A", "\u00f8": "o", "\u00d8": "O",
})


def _stylized(s: str) -> bool:
    """Latin text with a few Cyrillic or Greek look-alike letters in it."""
    latin = any("a" <= c <= "z" for c in s.lower())
    other = any("\u0370" <= c <= "\u04ff" for c in s)
    return latin and other


def fold(s: Optional[str]) -> str:
    """Case- and accent-insensitive form used for comparisons.

    Accents are dropped on Latin, Greek and Cyrillic letters only, so Japanese
    voicing marks (\u304b vs \u304c) are kept. Styled names compare as plain ones
    (``KoЯn`` == ``Korn``).
    """
    s = unicodedata.normalize("NFKC", s or "").translate(_INVISIBLE).translate(_QUOTES)
    if _stylized(s):
        s = s.translate(_LOOKALIKE)
    out: List[str] = []
    base = 0
    for ch in unicodedata.normalize("NFD", s):
        if unicodedata.combining(ch):
            if base < 0x0530:
                continue
        else:
            base = ord(ch)
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out)).casefold()


def norm(s: Optional[str]) -> str:
    """Comparison key: folded, punctuation and emojis removed, words separated by one space."""
    s = fold(s).replace("&", " and ").replace("+", " and ").replace("$", "s")
    s = s.replace("'", "").replace(".", "")
    chars = [c if unicodedata.category(c)[0] in "LNM" else " " for c in s]
    return " ".join("".join(chars).split())


def compact(s: Optional[str]) -> str:
    """Like :func:`norm` without spaces: ``KendrickLamar`` == ``Kendrick Lamar``."""
    return norm(s).replace(" ", "")


def dropped(s: Optional[str]) -> str:
    """Key with accented letters deleted, as old file names lost them.

    ``Inachev\u00e9s`` -> ``inachevs``, ``Tout L\u00e2cher`` -> ``toutlcher``, ``You\u2019re`` -> ``youre``.
    """
    s = unicodedata.normalize("NFC", fold_case_only(s))
    return re.sub(r"[^a-z0-9]", "", s)


def fold_case_only(s: Optional[str]) -> str:
    return unicodedata.normalize("NFKC", s or "").translate(_INVISIBLE).translate(_QUOTES).casefold()


_STOP = {"the", "a", "an", "and", "feat", "ft", "featuring", "with", "de", "la", "le", "les", "el"}


def tokens(s: Optional[str]) -> Set[str]:
    return {w for w in norm(s).split() if w not in _STOP}


def similarity(a: str, b: str) -> float:
    """0..1 similarity between two titles (1.0 for the same comparison key)."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb or na.replace(" ", "") == nb.replace(" ", ""):
        return 1.0
    ta, tb = tokens(na), tokens(nb)
    score = len(ta & tb) / max(1, len(ta | tb))
    short, long_ = sorted((na, nb), key=len)
    if len(short) >= 4 and short in long_:
        score = max(score, 0.8)
    return score


_PART = re.compile(r"\b(?:pt|part|partie|parte|teil|vol|volume|chapter|chap|no|n\u00ba|n\u00b0|#)\s*\.?\s*"
                   r"([ivx]+|\d+)\b", re.I)
_ROMAN = {"i": 1, "v": 5, "x": 10}


def _roman(s: str) -> int:
    total, prev = 0, 0
    for ch in reversed(s.lower()):
        v = _ROMAN[ch]
        total, prev = (total - v, prev) if v < prev else (total + v, v)
    return total


def part_numbers(title: Optional[str]) -> FrozenSet[int]:
    """``Pt. II`` / ``Part 2`` / ``Vol. 3`` -> {2} / {2} / {3}."""
    out = set()
    for m in _PART.finditer(fold(title)):
        v = m.group(1)
        out.add(int(v) if v.isdigit() else _roman(v))
    return frozenset(out)


# --------------------------------------------------------------------------------------
# Separators, brackets, noise and version words
# --------------------------------------------------------------------------------------

_HYPHENS = "\\-\u2010\u2011\u2012\u2212\ufe63\uff0d"
_DASHES = "\u2013\u2014\u2015\ufe58"
_BARS = "|\uff5c\u00a6\u2758"

# " - " needs spaces on both sides (so "Jay-Z" survives); en/em dashes split unless
# they sit between two digits (so "1975\u20131976" survives); bars and "//" always split.
SEPARATOR = re.compile(
    rf"\s+[{_HYPHENS}]+\s+"
    rf"|\s+[{_DASHES}]+\s*|\s*[{_DASHES}]+\s+|(?<!\d)[{_DASHES}]+(?!\d)"
    rf"|\s*[{_BARS}]+\s*"
    r"|\s*/{2,}\s*"
)

_OPEN = "([{\u3010\uff08\uff3b\u3014"
_CLOSE = ")]}\u3011\uff09\uff3d\u3015"
BRACKET = re.compile(
    rf"\s*([{re.escape(_OPEN)}])([^{re.escape(_OPEN + _CLOSE)}]*)([{re.escape(_CLOSE)}])"
)

NOISE = re.compile(
    r"\b(?:official|officiel(?:le)?|music\s+video|video\s*clip|videoclip|video|vid[e\u00e9]o|clip"
    r"|audio|lyrics?|paroles|letra|visuali[sz]er|m/?v|hd|hq|4k|1080p|720p"
    r"|remaster(?:ed)?|full\s+album|explicit|with\s+lyrics|color\s+coded"
    r"|subtitulad[oa]|legendado|sub\s+espa[n\u00f1]ol|eng(?:lish)?\s+sub(?:s|titles)?"
    r"|prod(?:uced)?\.?(?:\s+by)?\s+\S+|topic|eng|rom|han|kan|vostfr|subs?|subtitles)\b",
    re.I,
)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")

_TRAILING_NOISE = [
    re.compile(r"\s*[" + _HYPHENS + _DASHES + _BARS + r":]\s*(?:official\s+)?(?:music\s+|lyrics?\s+)?"
               r"(?:video|audio|visuali[sz]er|lyrics?|paroles|clip(?:\s+officiel)?|"
               r"vid[e\u00e9]o(?:\s+officielle)?|mv)\s*$", re.I),
    re.compile(r"\s+official\s+(?:music\s+)?(?:video|audio|visuali[sz]er|clip)\s*$", re.I),
    re.compile(r"\s+(?:lyrics?|music)\s+video\s*$", re.I),
    re.compile(r"\s+(?:with\s+)?lyrics\s*$", re.I),
    re.compile(r"\s+clip\s+officiel\s*$|\s+vid[e\u00e9]o\s+officielle\s*$", re.I),
    re.compile(r"\s+\(?(?:hd|hq|4k)\)?\s*$", re.I),
]

FEAT_BRACKET = re.compile(r"\s*[\(\[]\s*(?:feat|ft|featuring)\b\.?\s*([^\)\]]+)[\)\]]", re.I)
FEAT_BARE = re.compile(r"\s+(?:feat|ft|featuring)\b\.?\s+(.+?)(?=\s*[\(\[]|$)", re.I)

# Version words. They are only looked for inside brackets or in an extra " - " part,
# never in the title itself ("Live Forever" is a studio song).
QUALIFIERS = {
    "live": ["live", "en direct", "en vivo", "ao vivo", "in concert", "concert", "session", "sessions",
             "garage session", "tiny desk", "colors show", "a colors show", "colorsxstudios", "kexp",
             "audiotree", "boiler room", "bbc", "live lounge", "npr", "rehearsal", "dscvr", "vevo lift",
             "recorded at", "recorded in", "recorded live", "spotify session", "spotify sessions", "spotify singles",
             "deezer session", "deezer sessions", "apple music session", "studio session", "radio session",
             "take away show", "la blogotheque"],
    "acoustic": ["acoustic", "acoustique", "acustico", "ac\u00fastico", "unplugged", "stripped"],
    "remix": ["remix", "rmx", "mix", "rework", "re work", "flip", "vip", "bootleg", "mashup", "mash up",
              "refix", "dub"],
    "edit": ["edit", "radio edit", "extended", "short version", "re edit"],
    "cover": ["cover", "tribute"],
    "demo": ["demo", "early version", "alternate", "alternative version", "alt version", "outtake"],
    "instrumental": ["instrumental", "karaoke", "backing track", "inst"],
    "acapella": ["a cappella", "acappella", "acapella", "a capella", "vocals only"],
    "altered": ["sped up", "speed up", "nightcore", "slowed", "reverb", "8d", "daycore", "chopped"],
    "orchestral": ["orchestral", "symphonic", "piano version"],
}
_NEUTRAL = re.compile(r"\b(?:original|album|single|main|clean|dirty|explicit|radio)\s+(?:mix|version)\b")
_QUALIFIER_RES = {
    cls: re.compile(r"(?:^| )(?:" + "|".join(re.escape(norm(w)) for w in words) + r")(?: |$)")
    for cls, words in QUALIFIERS.items()
}


def qualifiers_in(text: str) -> Set[str]:
    """Version classes ("live", "remix"...) mentioned in a piece of text."""
    n = _NEUTRAL.sub(" ", norm(text))
    return {cls for cls, rx in _QUALIFIER_RES.items() if rx.search(n)}


def title_qualifiers(title: str) -> Set[str]:
    """Version classes of a title: from its brackets and extra " - " parts only."""
    found: Set[str] = set()
    for m in BRACKET.finditer(title or ""):
        found |= qualifiers_in(m.group(2))
    parts = SEPARATOR.split(title or "")
    for part in parts[1:]:
        found |= qualifiers_in(part)
    return found


_FILLER = {"at", "in", "from", "on", "for", "the", "a", "an", "version", "session", "sessions", "show",
           "concert", "recorded", "performance", "with", "de", "du", "en", "au", "a", "la", "le", "les",
           "and", "full", "band", "studio", "official", "edition"}
_VERSION_LEAD = re.compile(r"^(?:recorded\s+)?(?:live|session|sessions|acoustic|unplugged)\s+"
                           r"(?:at|in|from|on|for|@|\u00e0|au|en|aus|im)\b")


def is_version_part(part: str) -> bool:
    """True for a part like "Live at Wembley", "Garage Session", "Acoustic Version", "Official Video".

    "Live Forever" or "Title (Remix)" are not: they still have words of their own.
    """
    if _is_noise_only(part):
        return True
    n = _NEUTRAL.sub(" ", norm(BRACKET.sub(" ", part)))
    if not qualifiers_in(n):
        return False
    if _VERSION_LEAD.search(n):
        return True
    for rx in _QUALIFIER_RES.values():
        n = rx.sub(" ", f" {n} ")
        n = rx.sub(" ", f" {n} ")
    rest = [w for w in n.split() if w not in _FILLER and not _YEAR.fullmatch(w)]
    return not rest


def _is_noise_only(text: str) -> bool:
    rest = NOISE.sub(" ", text)
    rest = _YEAR.sub(" ", rest)
    return not norm(rest)


# Words removed from a version bracket: "(Official Live Video)" -> "(Live)".
VIDEO_WORDS = re.compile(r"\b(?:official|officiel(?:le)?|music\s+video|video|vid[e\u00e9]o|clip|audio|lyrics?|"
                         r"visuali[sz]er|explicit)\b", re.I)


def _clean_bracket(m: "re.Match[str]") -> str:
    """Drop brackets made only of noise; keep version brackets, minus their video words;
    keep anything else ("(Recorded at Deezer HQ, Paris)" is not noise because of "HQ")."""
    inner = m.group(2)
    if FEAT_BRACKET.match(m.group(0)) or not NOISE.search(inner):
        return m.group(0)
    if _is_noise_only(inner):
        return ""
    if qualifiers_in(inner):
        rest = " ".join(VIDEO_WORDS.sub(" ", inner).split()).strip(" -,/|:")
        return f" ({rest})" if rest else ""
    return m.group(0)


def strip_noise(s: str) -> Tuple[str, bool]:
    """Remove "(Official Video)", "[HD]", "- Lyrics"... Returns (text, changed)."""
    before = s
    s = BRACKET.sub(_clean_bracket, s)
    for _ in range(3):
        for rx in _TRAILING_NOISE:
            t = rx.sub("", s)
            if t.strip() and t != s:
                s = t
    s = " ".join(s.split()).strip(" -\u2013\u2014|:")
    return s, s != " ".join(before.split())


# --------------------------------------------------------------------------------------
# Artists
# --------------------------------------------------------------------------------------

_ARTIST_CHANNEL = re.compile(r"(?:\S(?:VEVO|Vevo)|\s*-\s*Topic|\s*\(?Official\)?)$", re.I)
_LABEL_CHANNEL = re.compile(r"(?:\s+(?:Music|Records|Recordings|TV|Lyrics|Channel|Entertainment|Sounds|Audio)"
                            r"|[a-z]edits?)$", re.I)
# Channels that post other people's music. Their name is never the artist.
CURATORS = {
    "colors", "colorsxstudios", "colors studios", "npr music", "nprmusic", "npr", "tiny desk", "kexp",
    "audiotree", "genius", "lyrical lemonade", "majestic casual", "proximity", "trap nation", "chill nation",
    "mrsuicidesheep", "cercle", "mahogany", "sofar sounds", "la blogotheque", "bbc radio 1", "bbc music",
    "vevo dscvr", "dscvr", "7clouds", "the vibe guide", "selected", "spinnin records", "monstercat",
    "ukf", "bass nation", "xkito music", "lofi girl", "chillhop music", "suicidesheep",
    "the sound you need", "various artists", "unknown artist", "artiste inconnu", "inconnu",
}
# Their uploads are live sessions, never the studio recording.
SESSION_CHANNELS = {"colors", "colorsxstudios", "colors studios", "npr music", "nprmusic", "npr", "tiny desk",
                    "kexp", "audiotree", "cercle", "mahogany", "sofar sounds", "la blogotheque", "bbc radio 1",
                    "bbc music", "boiler room"}
_CURATORS_COMPACT = {c.replace(" ", "") for c in CURATORS}
_SESSIONS_COMPACT = {c.replace(" ", "") for c in SESSION_CHANNELS}
_ARTIST_SPLIT = re.compile(
    r"\s*(?:,|;|&|\+|\s[x\u00d7]\s|\s(?:feat|ft|featuring)\b\.?|\swith\s|\set\s|\sand\s|\sy\s|\svs\.?\s)\s*",
    re.I,
)


def clean_artist(a: Optional[str]) -> str:
    """Strip YouTube channel suffixes: ``FooVEVO``, ``Foo - Topic``, ``Foo Official``.

    VEVO channel names glue the words together, so ``LilNasXVEVO`` gives ``Lil Nas X``.
    """
    a = tidy(a)
    if re.search(r"\S(?:VEVO|Vevo)$", a):
        a = re.sub(r"(?:VEVO|Vevo)$", "", a)
        if " " not in a:
            a = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", a)
    a = re.sub(r"\s*(?:VEVO|Vevo)$", "", a)
    a = re.sub(r"\s*-\s*Topic$", "", a, flags=re.I)
    a = re.sub(r"\s*\(?\bOfficial\)?$", "", a, flags=re.I)
    return a.strip()


def channel_kind(a: Optional[str]) -> Optional[str]:
    """``"artist"`` for an artist's own channel (``FooVEVO``, ``Foo - Topic``, ``Foo Official``),
    ``"curator"`` for a channel posting other people's music (``COLORS``, ``Foo Records``,
    ``NPR Music``, ``alfiedits``), else None."""
    a = tidy(a)
    if not a:
        return None
    if norm(a) in CURATORS or compact(a) in _CURATORS_COMPACT:
        return "curator"
    if _ARTIST_CHANNEL.search(a):
        return "artist"
    if _LABEL_CHANNEL.search(a):
        return "label"
    return None


def is_channel(a: Optional[str]) -> bool:
    """True when the artist field looks like a YouTube channel rather than an artist."""
    return channel_kind(a) is not None


def is_session_channel(a: Optional[str]) -> bool:
    return norm(a) in SESSION_CHANNELS or compact(a) in _SESSIONS_COMPACT


def primary_artist(a: Optional[str]) -> str:
    """First credited artist: ``A feat. B``, ``A & B``, ``A x B`` -> ``A``."""
    a = tidy(a)
    return _ARTIST_SPLIT.split(a, maxsplit=1)[0].strip() if a else ""


def script(s: Optional[str]) -> str:
    """``"latin"``, ``"other"`` (Japanese, Chinese, Korean, Cyrillic, Greek...) or ``""``."""
    letters = [c for c in fold(s) if unicodedata.category(c)[0] == "L"]
    if not letters:
        return ""
    latin = sum(1 for c in letters if ord(c) < 0x0250 or 0x1E00 <= ord(c) <= 0x1EFF)
    return "latin" if latin * 2 >= len(letters) else "other"


def different_script(a: Optional[str], b: Optional[str]) -> bool:
    """``Ryo Fukui`` and ``\u798f\u5c45\u826f`` cannot be compared letter by letter."""
    sa, sb = script(a), script(b)
    return bool(sa and sb and sa != sb)


def same_artist(a: Optional[str], b: Optional[str]) -> bool:
    """Same artist, comparing whole words only.

    ``Kendrick Lamar`` matches ``KendrickLamar`` and ``Kendrick Lamar, SZA``, but a short
    name such as ``((( O )))`` (``o``) only matches itself, never every name with an "o".
    """
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na.replace(" ", "") == nb.replace(" ", ""):
        return True
    pa, pb = compact(primary_artist(a)), compact(primary_artist(b))
    if pa and pa == pb:
        return True
    wa, wb = na.split(), nb.split()
    short, long_ = sorted((wa, wb), key=len)
    if len("".join(short)) < 3:
        return False
    return any(long_[i:i + len(short)] == short for i in range(len(long_) - len(short) + 1))


# --------------------------------------------------------------------------------------
# Titles
# --------------------------------------------------------------------------------------

def split_featuring(title: str) -> Tuple[str, List[str]]:
    """``Song feat X`` / ``Song (ft. X)`` -> (``Song``, [``X``])."""
    feats: List[str] = []

    def grab(m: "re.Match[str]") -> str:
        feats.append(m.group(1).strip())
        return ""

    title = FEAT_BRACKET.sub(grab, title)
    title = FEAT_BARE.sub(grab, title)
    return " ".join(title.split()), feats


_KANA_ONLY = re.compile(r"^[\u30a0-\u30ff\u31f0-\u31ff\uff65-\uff9f\u3000\u301c\uff5e~\s!?\uff01\uff1f\u30fb]+$")


def strip_gloss(title: str) -> str:
    """Drop a katakana reading added after a Latin title: ``Plastic Love (\u30d7\u30e9\u30b9\u30c6\u30a3\u30c3\u30af\u30fb\u30e9\u30f4)`` -> ``Plastic Love``."""
    t = tidy(title)
    if not re.search(r"[A-Za-z]", t):
        return t

    def bracket(m: "re.Match[str]") -> str:
        return "" if _KANA_ONLY.match(m.group(2).strip() or "x") else m.group(0)

    t = BRACKET.sub(bracket, t)
    parts = SEPARATOR.split(t)
    kept = [p for p in parts if not _KANA_ONLY.match(p.strip() or "x")]
    if kept and kept != parts:
        t = " - ".join(kept)
    return " ".join(t.split())


def core_text(title: str) -> str:
    """The title without brackets, featuring, version parts or katakana reading."""
    t, _ = split_featuring(strip_gloss(title))
    t = BRACKET.sub(" ", t)
    t = _PART.sub(" ", t)
    parts = SEPARATOR.split(t)
    kept = [parts[0]] + [p for p in parts[1:] if not _is_noise_only(p) and not qualifiers_in(p)]
    return " ".join(" ".join(kept).split())


def core_title(title: str) -> str:
    """Comparison key of a title: no brackets, no featuring, no extra " - " parts."""
    return norm(core_text(title))


def same_title(local: str, catalogue: str) -> bool:
    """Same song title, also when the local name lost its accents or apostrophes.

    ``Inachevs`` matches ``Inachev\u00e9s`` and ``Youre`` matches ``You\u2019re``. Part
    numbers must agree: ``Pt. II`` is not ``Pt. III``.
    """
    if part_numbers(local) != part_numbers(catalogue):
        return False
    a, b = core_title(local), core_title(catalogue)
    if not a or not b:
        return False
    if a == b or a.replace(" ", "") == b.replace(" ", ""):
        return True
    raw = core_text(local)
    if raw.isascii():
        d = dropped(core_text(catalogue))
        return len(d) >= 3 and d == dropped(raw)
    return False


@dataclass
class Parsed:
    """Best reading of a local track name."""

    artist: str
    title: str
    qualifiers: FrozenSet[str] = frozenset()
    featuring: List[str] = field(default_factory=list)
    readings: List[Tuple[str, str]] = field(default_factory=list)
    artist_in_title: bool = False
    noisy: bool = False
    album_hint: str = ""
    year_hint: str = ""
    track_hint: int = 0

    @property
    def core(self) -> str:
        return core_title(self.title)


def _is_decoration(ch: str) -> bool:
    return unicodedata.category(ch) in ("So", "Sk", "Mn", "Cf") or ch in "\ufe0f\u200d"


def _unquote(s: str) -> str:
    """Strip surrounding quotes and decorative emojis (``Song \U0001f525`` -> ``Song``)."""
    s = s.strip()
    if any(unicodedata.category(c)[0] in "LN" for c in s):
        while s and _is_decoration(s[-1]):
            s = s[:-1].rstrip()
        while s and _is_decoration(s[0]):
            s = s[1:].lstrip()
    return s.strip("\"'\u201c\u201d\u2018\u2019\u00ab\u00bb ").strip()


def _join_extra(title: str, extra: List[str]) -> Tuple[str, Set[str]]:
    quals: Set[str] = set()
    for seg in extra:
        seg = _unquote(seg)
        if not seg or _is_noise_only(seg):
            continue
        if is_version_part(seg):
            quals |= qualifiers_in(seg)
            cleaned, _ = strip_noise(seg)
            title += f" ({cleaned or seg})"
        else:
            title += f" - {seg}"
    return title, quals


_TRACK_NO = re.compile(r"^\s*(?:\[(\d{1,3})\]|\((\d{1,3})\)|(\d{1,3})\s*[.)]|(\d{1,3})\s*$)\s*")
_ALBUM_YEAR = re.compile(r"\s*[\(\[]\s*([^\(\)\[\]]*?)[\s,\-]*((?:19|20)\d{2})\s*[\)\]]")


def _track_number(seg: str) -> Tuple[str, int]:
    """``[01] Title`` / ``01. Title`` / ``(01) Title`` -> (``Title``, 1)."""
    m = _TRACK_NO.match(seg)
    if not m:
        return seg, 0
    n = next(int(g) for g in m.groups() if g)
    return seg[m.end():].strip(), n


def _album_year(s: str) -> Tuple[str, str, str]:
    """``Title (Album 1979)`` -> (``Title``, ``Album``, ``1979``); versions are left alone."""
    for m in _ALBUM_YEAR.finditer(s):
        words = m.group(1).strip()
        if qualifiers_in(words) or (words and NOISE.search(words)):
            continue
        return (s[: m.start()] + s[m.end():]).strip(), words, m.group(2)
    return s, "", ""


def _add_featuring(title: str, feats: List[str]) -> str:
    """Put "(feat. X)" right after the song name, before version brackets."""
    first = BRACKET.search(title)
    head, tail = (title[: first.start()], title[first.start():]) if first else (title, "")
    return f"{head.rstrip()} (feat. {', '.join(feats)}){tail}"


def parse(name: Optional[str], artist: Optional[str] = "") -> Parsed:
    """Read ``(artist, title, version words)`` from a Music track name and artist field.

    Handles ``Artist - Title``, ``Title - Artist``, ``Album - 01. Title``,
    ``[01] Title (Album 1979)``, channel names in the artist field (``FooVEVO``,
    ``COLORS``, ``Foo Records``) and every kind of dash and bar.
    """
    raw = tidy(name)
    kind = channel_kind(artist)
    field_artist = "" if kind == "curator" else clean_artist(artist)
    channel = kind is not None
    s = raw.replace("_", " ") if "_" in raw and " " not in raw else raw
    s, noisy = strip_noise(s)
    s, album_hint, year_hint = _album_year(s)
    segs = [_unquote(x) for x in SEPARATOR.split(s)]
    segs = [x for x in segs if x]
    if not segs:
        return Parsed(artist=field_artist, title=raw, readings=[(field_artist, raw)])

    track_hint = 0
    numbered = None
    for i, seg in enumerate(segs):
        rest, n = _track_number(seg)
        if n and (rest or i + 1 < len(segs)):
            track_hint, numbered = n, i
            if rest:
                segs[i] = rest
            else:
                del segs[i]
            break

    out_artist, title, extra = field_artist, segs[0], []
    readings: List[Tuple[str, str]] = []
    in_title = False
    if numbered is not None and numbered >= 1 and len(segs) > numbered:
        # "Album - 01. Title" or "Artist - Album - 01. Title"
        title, extra = segs[numbered], segs[numbered + 1:]
        before = segs[:numbered]
        if len(before) >= 2:
            out_artist, album_hint, in_title = before[0], before[1], True
        elif field_artist and same_artist(before[0], field_artist):
            in_title = True
        else:
            album_hint = before[0]
    elif len(segs) >= 2:
        head, body, extra = segs[0], segs[1], segs[2:]
        if field_artist and same_artist(head, field_artist):
            in_title = True
            prettier = " " in head and " " not in field_artist
            out_artist = head if (channel or prettier) else field_artist
            title = body
        elif field_artist and same_artist(body, field_artist):
            in_title = True
            title = head
        elif all(is_version_part(x) for x in segs[1:]):
            title, extra = head, segs[1:]
        elif not field_artist or channel:
            in_title = True
            out_artist, title = head, body
            readings.append((body, head))
        else:
            # "Lyrical Lemonade" + "Juice WRLD - All Girls Are The Same": the field is not
            # obviously a channel, so keep it and let the lookup try both readings.
            title = " - ".join(segs)
            readings += [(head, body), (body, head)]
            extra = []
    else:
        quoted = re.search(r"[\u300c\u300e\"\u201c](.+?)[\u300d\u300f\"\u201d]", raw)
        if quoted and not field_artist:
            before_q = raw[: quoted.start()].strip(" -:")
            if before_q:
                out_artist, title = before_q, quoted.group(1)
                in_title = True

    title, quals = _join_extra(_unquote(title), extra)
    title, feats = split_featuring(title)
    if in_title and out_artist != field_artist:
        out_artist, artist_feats = split_featuring(out_artist)
        feats += artist_feats
    if feats:
        title = _add_featuring(title, feats)
    quals |= title_qualifiers(title)
    if is_session_channel(artist):
        quals.add("live")
    readings.insert(0, (out_artist, title))
    if field_artist and (field_artist, title) not in readings:
        readings.append((field_artist, title))
    readings.append(("", title))
    seen, uniq = set(), []
    for a, t in readings:
        key = (norm(a), core_title(t))
        if key[1] and key not in seen:
            seen.add(key)
            uniq.append((a, t))
    return Parsed(
        artist=out_artist,
        title=" ".join(title.split()),
        qualifiers=frozenset(quals),
        featuring=feats,
        readings=uniq,
        artist_in_title=in_title,
        noisy=noisy,
        album_hint=album_hint,
        year_hint=year_hint,
        track_hint=track_hint,
    )


def needs_cleanup(name: str, artist: str) -> bool:
    """True when the name or artist field carries YouTube noise or the artist name."""
    p = parse(name, artist)
    return (p.noisy or p.artist_in_title or bool(p.track_hint or p.album_hint)
            or clean_artist(artist) != tidy(artist) or channel_kind(artist) == "curator")


_UNSAFE = re.compile(r'[\x00-\x1f/:\\*?"<>|]+')


def safe_filename(s: str, limit: int = 120) -> str:
    s = _UNSAFE.sub("-", tidy(s)).strip(" .-") or "untitled"
    return s[:limit].rstrip(" .-")
