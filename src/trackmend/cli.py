"""Command line: ``trackmend scan | tags | review | lyrics | dedup | split | reimport | undo``."""
from __future__ import annotations

import argparse
import sys
from typing import Callable, List, Optional, TextIO

from . import __version__, dedup, lyrics, reimport, review, scan, split, tags, undo
from .music import MusicApp, MusicError
from .net import Http, NetError
from .ops import Context, Library, Stop, relocate
from .store import Busy, Home

EXIT_BUSY = 75

DESCRIPTION = """\
Repair a macOS Music library made of local files (YouTube rips, old MP3s):
titles, artwork, album info, lyrics, duplicates and long files.

Every command only shows what it would do. Nothing is changed without --apply,
every change is backed up first, and "trackmend undo" puts it back.
"""


def _apply(p: argparse.ArgumentParser, what: str) -> None:
    p.add_argument("--apply", action="store_true", help=f"really {what} (default: only show)")


def _playlist(p: argparse.ArgumentParser) -> None:
    p.add_argument("--playlist", metavar="NAME", help="only the tracks of this playlist (good for a first try)")


def _yes(p: argparse.ArgumentParser) -> None:
    p.add_argument("--yes", action="store_true", help="do not ask before taking tracks out of Music")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trackmend", description=DESCRIPTION,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"trackmend {__version__}")
    p.add_argument("--home", metavar="DIR", help="where backups, cache and progress are kept")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    s = sub.add_parser("scan", help="report what is wrong with the library (read only)")
    _playlist(s)
    s.add_argument("--details", action="store_true", help="list the tracks of each problem")
    s.add_argument("--json", metavar="FILE", help="also save the findings as JSON")

    s = sub.add_parser("tags", help="clean titles, fill album info and artwork from Deezer")
    _playlist(s)
    _apply(s, "write the tags")
    s.add_argument("--all", action="store_true", help="look up every track, not only incomplete ones")
    s.add_argument("--no-artwork", action="store_true", help="do not add artwork")
    s.add_argument("--replace-artwork", action="store_true", help="replace artwork that is already there")
    s.add_argument("--no-itunes", action="store_true", help="do not ask the iTunes Search API")
    s.add_argument("--no-musicbrainz", action="store_true", help="do not ask MusicBrainz and the Cover Art Archive")
    s.add_argument("--no-youtube", action="store_true", help="do not propose YouTube thumbnails as artwork")
    s.add_argument("--no-lyrics", action="store_true",
                   help="do not look up again the lyrics of renamed tracks that had none")
    s.add_argument("--write-via", choices=("auto", "files", "music"), default="auto",
                   help="write into the files, or ask Music to do it (safer while other scripts run); "
                        "auto picks Music when another AppleScript is running")
    s.add_argument("--language", default="en", help="language of genre names (default: en)")
    s.add_argument("--limit", type=int, metavar="N", help="stop after N tracks")
    s.add_argument("--retry", action="store_true", help="look up again tracks that had no match")
    s.add_argument("--redo", action="store_true", help="ignore what earlier runs already did")
    s.add_argument("--report", metavar="FILE.csv", help="save the planned changes as CSV")

    s = sub.add_parser("review", help="look at doubtful matches, accept some, write them")
    s.add_argument("--accept", metavar="N,N-M|all", help="accept these numbers")
    s.add_argument("--reject", metavar="N,N-M|all", help="reject these numbers (they are not proposed again)")
    s.add_argument("--open", action="store_true", help="open the page with the artwork side by side")
    _apply(s, "write the accepted matches")
    s.add_argument("--no-lyrics", action="store_true",
                   help="do not look up again the lyrics of renamed tracks that had none")

    s = sub.add_parser("lyrics", help="add missing lyrics from LRCLIB")
    _playlist(s)
    _apply(s, "add the lyrics")
    s.add_argument("--track", metavar="ID|WORDS", help="only this track, written right away (even if it has lyrics)")
    s.add_argument("--limit", type=int, metavar="N", help="stop after N tracks")
    s.add_argument("--retry", action="store_true", help="look up again tracks that had no lyrics")
    s.add_argument("--redo", action="store_true", help="ignore what earlier runs already did")

    s = sub.add_parser("dedup", help="find the same song twice and keep the best copy")
    _playlist(s)
    _apply(s, "take the extra copies out of Music")
    _yes(s)

    s = sub.add_parser("split", help="cut a full album or a mix into one track per song")
    s.add_argument("track", help="persistent ID, or words from the artist and name of the track")
    _playlist(s)
    src = s.add_mutually_exclusive_group()
    src.add_argument("--chapters", metavar="URL|FILE", help="YouTube link or .info.json with chapters")
    src.add_argument("--tracklist", metavar="FILE", help="text file: one song per line")
    src.add_argument("--deezer-album", metavar="ID|URL", help="Deezer album, lined up on the silences")
    s.add_argument("--artist", help="album artist (default: from the source)")
    s.add_argument("--album", help="album title (default: from the source)")
    s.add_argument("--year")
    s.add_argument("--genre")
    s.add_argument("--cover", metavar="IMAGE", help="JPEG or PNG artwork for the new tracks")
    s.add_argument("--skip", metavar="N,N", help="track numbers to leave out (interludes, talk)")
    s.add_argument("--out", metavar="DIR", help="folder for the new files (default: ~/Music/trackmend/...)")
    s.add_argument("--keep-original", action="store_true", help="leave the long track in Music")
    _apply(s, "cut and add the tracks")
    _yes(s)

    s = sub.add_parser("reimport", help="add again tracks whose duration is wrong in Music")
    _playlist(s)
    _apply(s, "re-add the files")
    _yes(s)

    s = sub.add_parser("undo", help="put back what a run changed")
    s.add_argument("run", nargs="?", help="run to undo (default: the last one)")
    s.add_argument("--list", action="store_true", help="list the runs")
    _apply(s, "undo")
    _yes(s)
    return p


def main(argv: Optional[List[str]] = None, music: Optional[Library] = None,
         http_factory: Optional[Callable[[], Http]] = None, out: Optional[TextIO] = None,
         interactive: Optional[bool] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    home = Home(args.home)
    out = out or sys.stdout
    err = sys.stderr if out is sys.stdout else out

    def fail(msg: str, code: int = 1) -> int:
        print(f"trackmend: {msg}", file=err, flush=True)
        return code

    try:
        if music is None:
            music = MusicApp()
        ctx = Context(music=music, home=home, out=out, assume_yes=getattr(args, "yes", False),
                      interactive=interactive, http_factory=http_factory)
        if args.command == "scan":
            tracks = music.tracks(args.playlist)
            relocate(ctx, tracks)
            scan.report(ctx, tracks, details=args.details, as_json=args.json,
                        title=f" of playlist \"{args.playlist}\"" if args.playlist else "")
            return 0
        with home.lock(args.command):
            if args.command == "undo":
                return undo.run(ctx, args, argv)
            if args.command == "review":
                return review.run(ctx, args, argv)
            tracks = music.tracks(args.playlist)
            relocate(ctx, tracks)   # files replaced or moved since Music's own records were read
            if args.command == "tags":
                return tags.run(ctx, tracks, args, argv)
            if args.command == "lyrics":
                return lyrics.run(ctx, tracks, args, argv)
            if args.command == "dedup":
                return dedup.run(ctx, tracks, args.apply, argv)
            if args.command == "split":
                return split.run(ctx, tracks, args, argv)
            if args.command == "reimport":
                return reimport.run(ctx, tracks, args, argv)
    except Busy as e:
        return fail(f"another command is already working on the library: {e}. "
                    "Wait for it to finish (or stop it) and run this again.", EXIT_BUSY)
    except (Stop, MusicError, NetError) as e:
        return fail(str(e))
    except KeyboardInterrupt:
        return fail("stopped", 130)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
