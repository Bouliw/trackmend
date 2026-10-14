"""``trackmend tags``: clean titles, then fill album info and artwork from Deezer."""
from __future__ import annotations

import csv
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import audio, lyrics, text
from .artwork import ArtworkFinder, is_video_frame, squarify
from .confirm import Confirmer
from .deezer import DOUBTFUL, Deezer, Lookup
from .music import Track, other_scripts_running
from .net import NetError
from .ops import Context, Stop, Syncer, local_file, shorten, write_tags
from .review import ReviewStore
from .scan import NOT_MUSIC, YOUTUBE_GENRES

MIN_LEN, MAX_LEN = 30, 15 * 60


@dataclass
class Plan:
    track: Track
    changes: Dict[str, object] = field(default_factory=dict)
    cover_url: str = ""
    cover: Optional[bytes] = None
    cover_source: str = ""
    source: str = ""
    doubtful: str = ""                    # why the whole match must be reviewed first
    review_cover: Optional[bytes] = None  # artwork that must be looked at first
    review_cover_source: str = ""


def candidates(tracks: List[Track], everything: bool) -> List[Track]:
    out = []
    for t in tracks:
        if not local_file(t) or not os.path.exists(t.location or ""):
            continue
        if not (MIN_LEN <= t.duration <= MAX_LEN):
            continue
        if NOT_MUSIC.search(t.name) or NOT_MUSIC.search(os.path.basename(t.location)):
            continue
        if everything or needs_work(t):
            out.append(t)
    return sorted(out, key=lambda t: (t.artist.lower(), t.name.lower()))


def needs_work(t: Track) -> bool:
    if not t.artist.strip() or not t.album.strip() or text.needs_cleanup(t.name, t.artist):
        return True
    if text.norm(t.genre) in YOUTUBE_GENRES:
        return True
    try:
        cover = audio.cover_bytes(t.location)
    except audio.AudioError:
        return False
    return not cover or is_video_frame(cover)


def current_fields(t: Track, ftags: audio.Tags) -> Dict[str, object]:
    return {"title": t.name, "artist": t.artist, "album": t.album, "album_artist": t.album_artist,
            "year": str(t.year or ""), "genre": t.genre, "track": t.track_number or ftags.track,
            "track_total": ftags.track_total, "disc": ftags.disc}


def keep_artist(local: str, catalogue: str, local_is_channel: bool) -> str:
    """The catalogue's artist, unless the local one says more or is spelled the usual way.

    ``Roméo Elvis, Le Motel`` stays (the catalogue only credits the first artist) and
    ``KoЯn`` stays (the catalogue writes ``Korn``).
    """
    if not local or local_is_channel or not text.same_artist(local, catalogue):
        return catalogue
    if text.compact(local) != text.compact(catalogue) and \
            text.compact(text.primary_artist(local)) == text.compact(catalogue):
        return local
    if text.compact(local) == text.compact(catalogue) and local != catalogue and not local.isascii():
        return local
    return catalogue


def keep_featuring(title: str, featuring: List[str], artist: str) -> str:
    """Add back the guests the catalogue left out of the title."""
    _, already = text.split_featuring(title)
    missing = [f for f in featuring if not any(text.same_artist(f, a) for a in already)
               and not text.same_artist(f, artist) and text.norm(f) not in text.norm(artist)]
    return f"{title} (feat. {', '.join(missing)})" if missing else title


def plan_for(t: Track, ftags: audio.Tags, parsed: text.Parsed, lookup: Lookup) -> Plan:
    cur = current_fields(t, ftags)
    m = lookup.match
    if m and m.tier != "script" and parsed.artist and not text.same_artist(m.artist, parsed.artist):
        lookup = Lookup(None, "only another artist")   # never replace a known artist
    if lookup.match:
        m = lookup.match
        new: Dict[str, object] = m.fields()
        from_channel = text.is_channel(t.artist) and text.norm(parsed.artist) == text.norm(text.clean_artist(t.artist))
        new["artist"] = keep_artist(parsed.artist, m.artist, from_channel)
        new["title"] = keep_featuring(m.title, parsed.featuring, str(new["artist"]))
        source = f"deezer {m.tier}, ±{int(round(m.gap))} s"
        cover_url = m.cover_url
    else:
        new = {}
        if parsed.title and parsed.title != text.tidy(t.name):
            new["title"] = parsed.title
        if parsed.artist != text.tidy(t.artist) and (parsed.artist or text.channel_kind(t.artist) == "curator"):
            new["artist"] = parsed.artist
        if parsed.album_hint and not t.album.strip():
            new["album"] = parsed.album_hint
        if parsed.year_hint and not t.year:
            new["year"] = parsed.year_hint
        if parsed.track_hint and not cur["track"]:
            new["track"] = parsed.track_hint
        source = "cleanup only" + ("; the catalogue only has other versions" if lookup.rejected_versions else "")
        cover_url = ""
    if text.norm(t.genre) in YOUTUBE_GENRES and not new.get("genre"):
        new["genre"] = ""
    changes = {k: v for k, v in new.items() if str(v) != str(cur.get(k) or "")}
    return Plan(t, changes, cover_url, source=source)


def choose_artwork(plan: Plan, parsed: text.Parsed, finder: Optional[ArtworkFinder], http, covers_dir: str,
                   existing: Optional[bytes], replace: bool) -> None:
    """Fill ``plan.cover``, following the order described in artwork.py."""
    bad = existing is not None and is_video_frame(existing)
    wanted = existing is None or bad or replace
    if plan.cover_url and (wanted or plan.changes):
        data = http.download(plan.cover_url, covers_dir)
        if data:
            plan.cover, plan.cover_source = data, "Deezer"
            return
    if not wanted or finder is None:
        return
    artist = str(plan.changes.get("artist", plan.track.artist) or parsed.artist)
    title = str(plan.changes.get("title", parsed.title))
    album = str(plan.changes.get("album", plan.track.album))
    data, where, doubtful = finder.find(plan.track, artist, title, album, sorted(parsed.qualifiers))
    if data and is_video_frame(data):
        data = squarify(data) or data
    if data and doubtful:
        plan.review_cover, plan.review_cover_source = data, where
    elif data:
        plan.cover, plan.cover_source = data, where
    elif bad:
        fixed = squarify(existing)  # type: ignore[arg-type]
        if fixed:
            plan.cover, plan.cover_source = fixed, "video frame cut to a square"


def old_label(t: Track) -> str:
    """The name as it is, with the artist field only when the name does not already say it."""
    shown = text.clean_artist(t.artist)
    if t.artist and text.compact(shown) not in text.compact(t.name):
        return f"{t.name}  [{t.artist}]"
    return t.name


def describe(p: Plan) -> str:
    t, c = p.track, p.changes
    old = shorten(old_label(t), 64)
    artist = c.get("artist", t.artist)
    title = c.get("title", t.name)
    new = f"{artist} - {title}" if artist else str(title)
    extra = [str(c[k]) for k in ("album", "year") if c.get(k)]
    if "genre" in c and not c["genre"]:
        extra.append("genre emptied")
    if p.cover:
        extra.append(f"artwork: {p.cover_source}")
    tail = f"  [{', '.join(extra)}]" if extra else ""
    source = p.source if c else "tags unchanged"
    line = f"  {old}\n      \u2192 {shorten(new, 70)}{tail}  ({source})"
    if p.doubtful:
        line += f"\n      ? to review: {p.doubtful}"
    elif p.review_cover:
        line += f"\n      ? artwork to review: {p.review_cover_source}"
    return line


SAMPLES, SAMPLE_PAUSE = 6, 0.25


def pick_write_mode(ctx: Context, wanted: str) -> str:
    """``auto``: through Music when another script is driving apps right now, else into the files."""
    if wanted != "auto":
        return wanted
    if sys.platform != "darwin":
        return "files"
    for _ in range(SAMPLES):
        if other_scripts_running():
            ctx.say("Another script is driving an app with AppleScript; writing through Music, not into the files.")
            return "music"
        time.sleep(SAMPLE_PAUSE)
    return "files"


def run(ctx: Context, tracks: List[Track], args, argv: List[str]) -> int:
    apply = args.apply
    progress = ctx.home.progress("tags")
    done = {} if args.redo else progress.get()
    skip = {"written", "unchanged", "no-match", "review", "rejected"} - ({"no-match"} if args.retry else set())
    todo = [t for t in candidates(tracks, args.all) if done.get(t.pid) not in skip]
    if args.limit:
        todo = todo[: args.limit]
    ctx.say(f"{len(todo)} tracks to look up" + (" (dry run: nothing will be written)" if not apply else ""))
    via = pick_write_mode(ctx, args.write_via) if apply else "files"

    run_ = ctx.home.new_run("tags", argv) if apply else None
    syncer = Syncer(ctx, progress) if apply and via == "files" else None
    if syncer:
        for pid in progress.unsynced():
            syncer.add(pid, artwork="show")
        syncer.flush()

    http = ctx.http()
    dz = Deezer(http, language=args.language)
    finder = None if args.no_artwork else ArtworkFinder(
        http, ctx.home.covers_dir, tracks, itunes=not args.no_itunes, musicbrainz=not args.no_musicbrainz,
        youtube_thumbnails=not args.no_youtube)
    confirmer = Confirmer(http, tracks, itunes=not args.no_itunes, musicbrainz=not args.no_musicbrainz)
    review = ReviewStore(ctx.home.root)
    counts = {"matched": 0, "cleaned": 0, "unchanged": 0, "review": 0, "errors": 0}
    rows: List[Dict] = []
    renamed: List[str] = []   # their lyrics may be found now
    stopped = False
    try:
        for i, t in enumerate(todo, 1):
            try:
                ftags = audio.read(t.location)
                existing = audio.cover_bytes(t.location)
                parsed = text.parse(t.name, t.artist)
                lookup = lookup_track(dz, parsed, t)
                plan = plan_for(t, ftags, parsed, lookup)
                if lookup.match and lookup.match.tier in DOUBTFUL:
                    ok, why = confirmer.check(lookup.match, parsed.artist, parsed.title, t.duration, t)
                    if ok:
                        plan.source += f", confirmed: {why}"
                    else:
                        plan.doubtful = why
                if not args.no_artwork:
                    choose_artwork(plan, parsed, finder, http, ctx.home.covers_dir, existing, args.replace_artwork)
            except (audio.AudioError, NetError) as e:
                counts["errors"] += 1
                ctx.say(f"  ! {t.label()}: {e}")
                if apply:
                    progress.set(t.pid, "error", str(e))
                continue
            if not (plan.changes or plan.cover or plan.review_cover):
                counts["unchanged"] += 1
                if apply:
                    progress.set(t.pid, "no-match" if not lookup.match else "unchanged")
                continue
            ctx.say(describe(plan))
            rows.append(_row(plan))
            before = current_fields(t, ftags)
            if plan.doubtful:
                counts["review"] += 1
                if apply:
                    review.add(t, plan.changes, plan.cover, plan.cover_source, plan.doubtful, plan.source,
                               before={k: before.get(k) for k in plan.changes})
                    progress.set(t.pid, "review", plan.doubtful)
                continue
            if plan.review_cover:
                counts["review"] += 1
                if apply:
                    review.add(t, {}, plan.review_cover, plan.review_cover_source,
                               "artwork from a video thumbnail: check it", "")
            if not (plan.changes or plan.cover):
                if apply:
                    progress.set(t.pid, "review", "artwork")
                continue
            counts["matched" if lookup.match else "cleaned"] += 1
            if not apply:
                if lyrics.changed_names(plan.changes):
                    renamed.append(t.pid)
                continue
            try:
                write_tags(ctx, run_, t, plan.changes, plan.cover, via=via,
                           before={k: before.get(k) for k in plan.changes})
            except (Stop, audio.AudioError, OSError) as e:
                counts["errors"] += 1
                ctx.say(f"    ! not written: {e}")
                progress.set(t.pid, "error", str(e))
                continue
            progress.set(t.pid, "written", plan.source, synced=syncer is None)
            if lyrics.changed_names(plan.changes):
                renamed.append(t.pid)
            if syncer:
                syncer.add(t.pid, {k: "" for k, v in plan.changes.items() if v in (None, "")},
                           artwork="show" if plan.cover else "")
            if i % 50 == 0:
                ctx.say(f"  ... {i}/{len(todo)}")
    except KeyboardInterrupt:
        stopped = True
        ctx.say("\nStopped. Everything written so far is saved; run the same command to continue.")
    finally:
        if syncer:
            syncer.flush()
        progress.close()
    if args.report:
        _write_report(args.report, rows)
    verb = "written" if apply else "to write"
    ctx.say("")
    ctx.say(f"{counts['matched']} matched on Deezer, {counts['cleaned']} cleaned only, "
            f"{counts['unchanged']} left as they were, {counts['errors']} errors ({verb}).")
    if counts["review"]:
        ctx.say(f"{counts['review']} doubtful matches " + ("wait for you: trackmend review --open" if apply
                                                        else "will wait for review (nothing is written for them)."))
    if run_:
        ctx.say(f"Backup and journal: {run_.path}")
        ctx.say(f"Undo with: trackmend undo {run_.id} --apply")
    elif counts["matched"] or counts["cleaned"]:
        ctx.say("Nothing was changed. Run again with --apply to write these tags.")
    if renamed and not stopped and not args.no_lyrics:
        lyrics.search_again(ctx, renamed, "tags", apply=apply)
    return 0


def lookup_track(dz: Deezer, parsed: text.Parsed, t: Track) -> Lookup:
    """Deezer entry for a track. With a known artist, only that artist's songs can match.

    The search by title and length alone is kept for tracks without an artist, or whose
    artist field only held a YouTube channel name: a known artist (France Gall) is never
    replaced by another one (a cover can have nearly the same length).
    """
    has_artist = any(a for a, _ in parsed.readings)
    if has_artist:
        res = dz.find(parsed, t.duration, full_name=f"{t.artist} {t.name}")
        if res.match or res.rejected_versions:
            return res
    if not parsed.artist and not parsed.qualifiers and not known_artist(t.artist):
        return dz.find_by_title(parsed.title, t.duration)
    return Lookup(None, "no match")


def known_artist(artist: str) -> str:
    """The artist field, unless it is empty or a channel name ("Inconnu", "Lofi Girl")."""
    a = text.clean_artist(artist or "")
    return "" if not a or text.channel_kind(artist) == "curator" else a


def _row(p: Plan) -> Dict:
    r = {"persistent_id": p.track.pid, "old_artist": p.track.artist, "old_title": p.track.name,
         "source": p.source, "artwork": p.cover_source or p.review_cover_source, "review": p.doubtful}
    for k in audio.FIELDS:
        r[k] = p.changes.get(k, "")
    return r


def _write_report(path: str, rows: List[Dict]) -> None:
    cols = ["persistent_id", "old_artist", "old_title", *audio.FIELDS, "artwork", "source", "review"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
