"""``trackmend split``: cut a long file (full album, mix) into one track per song.

Where the cuts go comes from one of:

* the chapters of a YouTube video (metadata only, read with yt-dlp; nothing is downloaded),
* a text track list, with start times, durations, or only titles,
* the track list of a Deezer album, lined up on the silences of the file.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from . import audio, text
from .deezer import album_id_from, album_tracks
from .music import Track
from .ops import Context, Stop, add_files, fmt_duration, remove_track, shorten

Silence = Tuple[float, float]

_TS = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?![\d:])")
_NUMBERING = re.compile(r"^\s*(?:\d{1,3}\s*[.)\-:]\s*|\d{1,3}\s+(?=\D))")
SNAP_WINDOW = 10.0
MIN_TRACK = 3


@dataclass
class Segment:
    start: float
    end: float
    title: str
    artist: str = ""

    @property
    def length(self) -> float:
        return self.end - self.start


def _seconds(m: "re.Match[str]") -> int:
    h, mnt, s = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + int(s)


def _clean_line(s: str) -> str:
    s = _NUMBERING.sub("", s)
    s = re.sub(r"[\(\[]\s*[\)\]]", "", s)
    return s.strip(" -–—|:.\t")


# --------------------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------------------

def chapters_info(source: str) -> dict:
    """yt-dlp's JSON for a video (``--skip-download``), or a saved ``.info.json`` file."""
    if os.path.exists(source):
        with open(source, encoding="utf-8") as fh:
            return json.load(fh)
    if not shutil.which("yt-dlp"):
        raise Stop("reading chapters from a link needs yt-dlp (brew install yt-dlp), "
                   "or pass a saved .info.json file instead")
    r = subprocess.run(["yt-dlp", "--dump-single-json", "--skip-download", "--no-playlist", "--no-warnings", source],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise Stop(f"yt-dlp could not read {source}: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout)


def from_chapters(info: dict, total: float) -> List[Segment]:
    ch = info.get("chapters") or []
    if len(ch) < 2:
        raise Stop("this video has no chapters")
    scale = total / float(info["duration"]) if info.get("duration") else 1.0
    segs = []
    for i, c in enumerate(ch):
        start = float(c.get("start_time") or 0) * scale
        end = float(c.get("end_time") or (ch[i + 1]["start_time"] if i + 1 < len(ch) else total)) * scale
        segs.append(Segment(start, min(end, total), _clean_line(c.get("title") or f"Track {i + 1}")))
    segs[-1].end = total
    return segs


def from_tracklist(lines: Sequence[str], total: float,
                   silences: Optional[Callable[[], List[Silence]]] = None) -> List[Segment]:
    """Lines with start times (``00:00 Intro``), durations (``Intro 3:45``) or titles only."""
    entries: List[Tuple[Optional[int], str]] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        stamps = list(_TS.finditer(line))
        if not stamps:
            entries.append((None, _clean_line(line)))
            continue
        m = stamps[0] if stamps[0].start() <= 3 else stamps[-1]
        title = _clean_line(line[: m.start()] + " " + line[m.end():])
        entries.append((_seconds(m), title))
    if len(entries) < 2:
        raise Stop("the track list needs at least two lines")
    values = [v for v, _ in entries]
    titles = [t or f"Track {i + 1}" for i, (_, t) in enumerate(entries)]
    if all(v is None for v in values):
        if silences is None:
            raise Stop("a list of titles without times needs the audio file to find the silences")
        cuts = longest_gaps(silences(), len(titles) - 1, total)
        bounds = [0.0] + cuts + [total]
    elif any(v is None for v in values):
        raise Stop("some lines have a time and some do not")
    else:
        vals = [float(v) for v in values]  # type: ignore[arg-type]
        increasing = all(b > a for a, b in zip(vals, vals[1:]))
        if increasing and vals[0] <= 5 and vals[-1] < total:
            bounds = [0.0] + vals[1:] + [total]
        elif abs(sum(vals) - total) <= max(15.0, 0.03 * total):
            bounds = [0.0]
            for v in vals:
                bounds.append(bounds[-1] + v)
            bounds[-1] = total
        elif increasing and vals[-1] < total:
            bounds = vals + [total]
        else:
            raise Stop("the times are neither start times nor durations adding up to the file length")
    return [Segment(bounds[i], bounds[i + 1], titles[i]) for i in range(len(titles))]


def from_deezer(tracks: List[dict], total: float, silences: List[Silence]) -> List[Segment]:
    durations = [float(t.get("duration") or 0) for t in tracks]
    if not durations or not all(durations):
        raise Stop("this Deezer album has no track durations")
    s = sum(durations)
    scale = total / s if 0.9 <= total / s <= 1.1 else 1.0
    bounds = [0.0]
    for d in durations[:-1]:
        bounds.append(bounds[-1] + d * scale)
    bounds = [0.0] + snap(bounds[1:], silences) + [total]
    segs = []
    for i, t in enumerate(tracks):
        segs.append(Segment(bounds[i], bounds[i + 1], t.get("title") or f"Track {i + 1}",
                            (t.get("artist") or {}).get("name", "")))
    return segs


# --------------------------------------------------------------------------------------
# Silences
# --------------------------------------------------------------------------------------

def detect_silences(path: str, noise_db: float = -45, min_len: float = 0.7) -> List[Silence]:
    if not shutil.which("ffmpeg"):
        raise Stop("split needs ffmpeg (brew install ffmpeg)")
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", path, "-af",
                        f"silencedetect=noise={noise_db}dB:d={min_len}", "-f", "null", "-"],
                       capture_output=True, text=True)
    out: List[Silence] = []
    start = None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and start is not None:
            out.append((start, float(m.group(1))))
            start = None
    return out


def cut_point(s: Silence) -> float:
    """Where to cut inside a silence: just before the next song starts."""
    a, b = s
    return max(a, b - min(0.3, (b - a) / 2))


def snap(bounds: List[float], silences: List[Silence], window: float = SNAP_WINDOW) -> List[float]:
    out = []
    for b in bounds:
        near = [s for s in silences if abs(cut_point(s) - b) <= window]
        out.append(cut_point(min(near, key=lambda s: abs(cut_point(s) - b))) if near else b)
    return out


def longest_gaps(silences: List[Silence], n: int, total: float) -> List[float]:
    inner = [s for s in silences if s[0] > 1.0 and s[1] < total - 1.0]
    if len(inner) < n:
        raise Stop(f"found {len(inner)} silences for {n + 1} titles; give times in the track list")
    chosen = sorted(inner, key=lambda s: s[1] - s[0], reverse=True)[:n]
    return sorted(cut_point(s) for s in chosen)


# --------------------------------------------------------------------------------------
# Naming and cutting
# --------------------------------------------------------------------------------------

def name_segments(segs: List[Segment], default_artist: str) -> None:
    """Chapters like "Artist - Title" give one artist per track."""
    with_sep = sum(1 for s in segs if text.SEPARATOR.search(s.title))
    per_track = with_sep >= 0.6 * len(segs)
    for s in segs:
        if per_track and not s.artist:
            p = text.parse(s.title, "")
            s.artist, s.title = p.artist or default_artist, p.title
        else:
            s.title = text.parse(s.title, default_artist).title if s.title else s.title
            s.artist = s.artist or default_artist


def cut(src: str, seg: Segment, dst: str) -> None:
    cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{seg.start:.3f}", "-i", src,
           "-t", f"{seg.length:.3f}", "-map", "0:a:0", "-c", "copy", "-map_metadata", "-1"]
    if audio.ext(dst) in audio.MP4_EXT:
        # Without these, Music can read a wrong duration from a cut AAC file.
        cmd += ["-movflags", "+faststart", "-avoid_negative_ts", "make_zero"]
    subprocess.run(cmd + [dst], check=True, capture_output=True)


def default_folder(artist: str, album: str) -> str:
    return os.path.join(os.path.expanduser("~/Music/trackmend"), text.safe_filename(artist or "Unknown Artist"),
                        text.safe_filename(album or "Unknown Album"))


def find_track(tracks: List[Track], query: str) -> Track:
    q = query.strip()
    by_id = [t for t in tracks if t.pid.upper() == q.upper()]
    if by_id:
        return by_id[0]
    nq = text.norm(q)
    hits = [t for t in tracks if nq and nq in text.norm(f"{t.artist} {t.name}")]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise Stop(f"no track matches '{query}'")
    listing = "\n".join(f"  {t.pid}  {shorten(t.label(), 60)}  ({fmt_duration(t.duration)})" for t in hits[:20])
    raise Stop(f"{len(hits)} tracks match '{query}'; pass the persistent ID instead:\n{listing}")


def run(ctx: Context, tracks: List[Track], args, argv: List[str]) -> int:
    t = find_track(tracks, args.track)
    src = ctx.music.location(t.pid)
    if not src or not os.path.exists(src):
        raise Stop("the file of this track is missing")
    if not audio.is_supported(src):
        raise Stop(f"{audio.ext(src)} files are not handled")
    total = audio.length(src)
    meta: dict = {}
    if args.chapters:
        segs = from_chapters(chapters_info(args.chapters), total)
    elif args.tracklist:
        with open(args.tracklist, encoding="utf-8") as fh:
            segs = from_tracklist(fh.read().splitlines(), total, silences=lambda: detect_silences(src))
    elif args.deezer_album:
        meta, dz_tracks = album_tracks(ctx.http(), album_id_from(args.deezer_album))
        segs = from_deezer(dz_tracks, total, detect_silences(src))
    else:
        raise Stop("say where the cuts are: --chapters, --tracklist or --deezer-album")

    parsed = text.parse(t.name, t.artist)
    artist = args.artist or (meta.get("artist") or {}).get("name") or text.clean_artist(t.artist) or parsed.artist
    album = args.album or meta.get("title") or t.album or parsed.title
    year = args.year or (meta.get("release_date") or "")[:4] or (str(t.year) if t.year else "")
    genre = args.genre or next((g.get("name") for g in (meta.get("genres") or {}).get("data", [])), "") or t.genre
    name_segments(segs, artist)
    skip = {int(x) for x in (args.skip or "").replace(" ", "").split(",") if x}
    keep = [(i, s) for i, s in enumerate(segs, 1) if i not in skip]

    ctx.say(f"{t.label()}  ({fmt_duration(total)})")
    ctx.say(f"  → {len(keep)} tracks, album \"{album}\" by {artist}" + (f", {year}" if year else ""))
    for i, s in enumerate(segs, 1):
        mark = "skip" if i in skip else f"{i:>4}"
        ctx.say(f"  {mark}  {fmt_duration(s.start):>8} – {fmt_duration(s.end):>8}  ({fmt_duration(s.length)})  "
                f"{shorten(s.artist + ' - ' + s.title if s.artist != artist else s.title, 60)}")
    bad = [s for s in segs if s.length < MIN_TRACK]
    if bad:
        raise Stop(f"some tracks would be shorter than {MIN_TRACK} seconds; check the times")
    folder = args.out or default_folder(artist, album)
    if not args.apply:
        ctx.say(f"\nNothing was changed. With --apply the files go to {folder}, are added to Music"
                + ("" if args.keep_original else ", and the long file is taken out of Music (its file is kept)") + ".")
        return 0
    question = f"Split into {len(keep)} tracks" + ("" if args.keep_original else " and take the long file out of Music") + "?"
    if not ctx.confirm(question):
        return 0

    cover = None
    if args.cover:
        with open(args.cover, "rb") as fh:
            cover = fh.read()
    elif meta.get("cover_xl"):
        cover = ctx.http().download(meta["cover_xl"], ctx.home.covers_dir)
    if not cover:
        cover = audio.cover_bytes(src)

    run_ = ctx.home.new_run("split", argv)
    os.makedirs(folder, exist_ok=True)
    made: List[str] = []
    n = len(keep)
    for k, (i, s) in enumerate(keep, 1):
        dst = os.path.join(folder, f"{k:02d} {text.safe_filename(s.title, 100)}{audio.ext(src)}")
        cut(src, s, dst)
        audio.write(dst, {"title": s.title, "artist": s.artist, "album": album, "album_artist": artist,
                          "year": year, "genre": genre, "track": k, "track_total": n}, cover)
        got = audio.length(dst)
        if abs(got - s.length) > 1.5:
            ctx.say(f"  ! {os.path.basename(dst)}: {fmt_duration(got)} instead of {fmt_duration(s.length)}")
        made.append(dst)
    added = add_files(ctx, run_, made)
    failed = [p for p, pid in added if not pid]
    if failed:
        raise Stop(f"Music did not accept {len(failed)} files; the long track was left in place. "
                   f"Undo with: trackmend undo {run_.id} --apply")
    info = ctx.music.info([pid for _, pid in added if pid])
    wrong = []
    for (path, pid), (_, s) in zip(added, keep):
        d = info.get(pid or "") or {}
        if d.get("duration") and abs(float(d["duration"]) - s.length) > 3:
            wrong.append(pid)
        loc = d.get("location")
        if loc and os.path.exists(loc) and os.path.exists(path) and not os.path.samefile(loc, path):
            os.remove(path)  # Music copied the file into its own folder
    if wrong:
        ctx.say(f"  ! Music shows a wrong duration for {len(wrong)} new tracks; run: trackmend reimport --apply")
    if not args.keep_original:
        remove_track(ctx, run_, t, reason="split")
    ctx.say(f"Done: {len(made)} tracks added. Undo with: trackmend undo {run_.id} --apply")
    return 0
