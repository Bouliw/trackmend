"""``trackmend scan``: a read-only health report of the library."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections import OrderedDict
from typing import Dict, List, Optional

from . import audio, text
from .artwork import image_size, is_video_frame
from .dedup import find_duplicates
from .music import Track
from .ops import LONG_FILE, MIN_SONG, Context, fmt_duration, local_file, shorten

NOT_MUSIC = re.compile(
    r"\b(?:sfx|sound\s*effects?|whoosh|woosh|swoosh|riser|impact|transition|notification|ringtone|"
    r"voice\s*memo|new\s*recording|screen\s*recording|podcast|audiobook|chapter\s*\d+)\b"
    r"|^(?:dji|img|vid|mvi|gopr|pxl|rec)[_-]?\d{3,}|temp_recording|mixkit",
    re.I,
)
DURATION_TOLERANCE = 3.0

# YouTube video categories that end up in the genre field of downloaded files.
YOUTUBE_GENRES = {text.norm(g) for g in (
    "Music", "People & Blogs", "Gaming", "Entertainment", "Film & Animation", "Comedy", "Education",
    "Howto & Style", "News & Politics", "Nonprofits & Activism", "Science & Technology", "Sports",
    "Travel & Events", "Autos & Vehicles", "Pets & Animals", "Shows", "Trailers", "Movies")}

CHECKS = OrderedDict([
    ("artwork", "Missing artwork"),
    ("video_artwork", "Artwork is a video frame"),
    ("artist", "Missing artist"),
    ("album", "Missing album"),
    ("noisy", "Title with video noise"),
    ("artist_in_title", "Artist written in the title"),
    ("channel", "YouTube channel as artist"),
    ("youtube_genre", "YouTube category as genre"),
    ("duplicates", "Duplicate copies"),
    ("duration", "Wrong duration in Music"),
    ("lyrics", "Missing lyrics"),
    ("long", "Long files (albums, mixes)"),
    ("not_music", "Probably not music"),
    ("missing", "Missing files"),
    ("unsupported", "Format not handled"),
])

HINTS = {
    "artwork": "tags", "video_artwork": "tags", "youtube_genre": "tags", "artist": "tags", "album": "tags", "noisy": "tags", "artist_in_title": "tags",
    "channel": "tags", "duplicates": "dedup", "duration": "reimport", "lyrics": "lyrics", "long": "split",
}


def probe_length(path: str) -> Optional[float]:
    if not shutil.which("ffprobe"):
        return None
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def real_length(path: str, music_duration: float) -> float:
    """File duration. ffprobe settles the cases where mutagen and Music disagree."""
    n = audio.length(path)
    if abs(n - music_duration) > DURATION_TOLERANCE:
        p = probe_length(path)
        if p:
            return p
    return n


def wrong_duration(t: Track) -> Optional[float]:
    """Real length of the file when Music shows something else, else None."""
    if not local_file(t) or not os.path.exists(t.location or ""):
        return None
    try:
        n = real_length(t.location, t.duration)
    except audio.AudioError:
        return None
    return n if n and abs(n - t.duration) > DURATION_TOLERANCE else None


def analyse(tracks: List[Track]) -> Dict[str, List[Dict]]:
    found: Dict[str, List[Dict]] = {k: [] for k in CHECKS}

    def add(key: str, t: Track, note: str = "") -> None:
        found[key].append({"pid": t.pid, "label": t.label(), "note": note})

    for t in tracks:
        if not t.location or not os.path.exists(t.location):
            add("missing", t, t.location or "")
            continue
        if not audio.is_supported(t.location):
            add("unsupported", t, audio.ext(t.location))
            continue
        try:
            tags = audio.read(t.location)
        except audio.AudioError as e:
            add("unsupported", t, str(e))
            continue
        real = wrong_duration(t)
        if real:
            add("duration", t, f"Music {fmt_duration(t.duration)}, file {fmt_duration(real)}")
        length = real or t.duration
        name = os.path.basename(t.location)
        if length < MIN_SONG or NOT_MUSIC.search(t.name) or NOT_MUSIC.search(name):
            add("not_music", t, fmt_duration(length))
            continue
        if length > LONG_FILE:
            add("long", t, fmt_duration(length))
        if not tags.has_cover:
            add("artwork", t)
        elif is_video_frame(audio.cover_bytes(t.location)):
            size = image_size(audio.cover_bytes(t.location))
            add("video_artwork", t, f"{size[0]}x{size[1]}" if size else "")
        if not t.artist.strip():
            add("artist", t)
        if not t.album.strip():
            add("album", t)
        p = text.parse(t.name, t.artist)
        if p.noisy:
            add("noisy", t, p.title)
        if p.artist_in_title:
            add("artist_in_title", t, f"{p.artist} / {p.title}")
        kind = text.channel_kind(t.artist)
        if kind in ("artist", "curator") or (kind == "label" and p.artist_in_title):
            add("channel", t, t.artist)
        if text.norm(t.genre) in YOUTUBE_GENRES:
            add("youtube_genre", t, t.genre)
        if not t.lyrics_len and length <= LONG_FILE and "instrumental" not in p.qualifiers:
            add("lyrics", t)

    skip = {x["pid"] for k in ("not_music", "missing", "unsupported") for x in found[k]}
    songs = [t for t in tracks if local_file(t) and t.pid not in skip]
    for group in find_duplicates(songs).groups:
        for extra in group.remove:
            found["duplicates"].append({"pid": extra.pid, "label": extra.label(),
                                        "note": f"copy of {group.keep.pid}"})
    return found


def report(ctx: Context, tracks: List[Track], details: bool = False, as_json: Optional[str] = None,
           title: str = "") -> Dict[str, List[Dict]]:
    found = analyse(tracks)
    local = sum(1 for t in tracks if t.location)
    ctx.say(f"trackmend scan{title} — {len(tracks)} tracks, {local} local files")
    ctx.say("")
    width = max(len(v) for v in CHECKS.values()) + 3
    for key, label in CHECKS.items():
        n = len(found[key])
        hint = f"   → trackmend {HINTS[key]}" if n and key in HINTS else ""
        ctx.say(f"  {label + ' ':.<{width}} {n:>5}{hint}")
        if details and n:
            for item in found[key][:500]:
                note = f"  ({item['note']})" if item["note"] else ""
                ctx.say(f"      {shorten(item['label'], 70)}{note}")
    if not details:
        ctx.say("")
        ctx.say("Nothing was changed. Add --details to list the tracks.")
    if as_json:
        with open(as_json, "w", encoding="utf-8") as fh:
            json.dump(found, fh, ensure_ascii=False, indent=1)
    return found
