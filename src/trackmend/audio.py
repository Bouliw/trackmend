"""Reading, writing, saving and restoring the tags of audio files (MP3, AAC/ALAC, AIFF)."""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import mutagen
from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, USLT
from mutagen.mp4 import MP4Cover, MP4FreeForm

ID3_EXT = {".mp3", ".aif", ".aiff"}
MP4_EXT = {".m4a", ".m4b", ".mp4", ".m4p"}
SUPPORTED = (ID3_EXT | MP4_EXT) - {".m4p"}

FIELDS = ("title", "artist", "album", "album_artist", "year", "genre", "track", "track_total", "disc")

_MP4_TEXT = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "album_artist": "aART",
             "year": "\xa9day", "genre": "\xa9gen", "lyrics": "\xa9lyr"}
_ID3_TEXT = {"title": TIT2, "artist": TPE1, "album": TALB, "album_artist": TPE2, "year": TDRC, "genre": TCON}


class AudioError(Exception):
    pass


def ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def is_supported(path: Optional[str]) -> bool:
    return bool(path) and ext(path) in SUPPORTED


@dataclass
class Tags:
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    year: str = ""
    genre: str = ""
    track: int = 0
    track_total: int = 0
    disc: int = 0
    lyrics: str = ""
    has_cover: bool = False
    length: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in FIELDS}


def _open(path: str):
    try:
        f = mutagen.File(path)
    except Exception as e:  # mutagen raises many different error types
        raise AudioError(f"cannot read {os.path.basename(path)}: {e}") from e
    if f is None:
        raise AudioError(f"unknown audio format: {os.path.basename(path)}")
    return f


def _pair(value: str) -> tuple:
    head, _, tail = (value or "").partition("/")
    try:
        a = int(head.strip() or 0)
    except ValueError:
        a = 0
    try:
        b = int(tail.strip() or 0)
    except ValueError:
        b = 0
    return a, b


def read(path: str) -> Tags:
    f = _open(path)
    t = Tags(length=float(getattr(f.info, "length", 0.0) or 0.0))
    tags = f.tags
    if tags is None:
        return t
    if ext(path) in MP4_EXT:
        def first(key: str) -> str:
            v = tags.get(key)
            return str(v[0]) if v else ""
        t.title, t.artist, t.album = first("\xa9nam"), first("\xa9ART"), first("\xa9alb")
        t.album_artist, t.year, t.genre = first("aART"), first("\xa9day")[:4], first("\xa9gen")
        t.lyrics = first("\xa9lyr")
        if tags.get("trkn"):
            t.track, t.track_total = tags["trkn"][0]
        if tags.get("disk"):
            t.disc = tags["disk"][0][0]
        t.has_cover = bool(tags.get("covr"))
    else:
        def text(fid: str) -> str:
            fr = tags.get(fid)
            return str(fr.text[0]) if fr is not None and getattr(fr, "text", None) else ""
        t.title, t.artist, t.album, t.album_artist = text("TIT2"), text("TPE1"), text("TALB"), text("TPE2")
        t.year = (text("TDRC") or text("TYER"))[:4]
        t.genre = text("TCON")
        t.track, t.track_total = _pair(text("TRCK"))
        t.disc = _pair(text("TPOS"))[0]
        lyr = tags.getall("USLT")
        t.lyrics = str(lyr[0].text) if lyr else ""
        t.has_cover = bool(tags.getall("APIC"))
    return t


def length(path: str) -> float:
    return float(getattr(_open(path).info, "length", 0.0) or 0.0)


def image_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    raise AudioError("artwork must be JPEG or PNG")


def write(path: str, changes: Dict[str, Any], cover: Optional[bytes] = None) -> None:
    """Apply ``changes`` (field -> value; ``None`` or ``""`` removes the field) and artwork."""
    f = _open(path)
    if f.tags is None:
        f.add_tags()
    tags = f.tags
    if ext(path) in MP4_EXT:
        for k, key in _MP4_TEXT.items():
            if k in changes:
                v = changes[k]
                if v in (None, "", 0):
                    tags.pop(key, None)
                else:
                    tags[key] = [str(v)]
        if "track" in changes or "track_total" in changes:
            cur = (tags.get("trkn") or [(0, 0)])[0]
            n = int(changes.get("track", cur[0]) or 0)
            total = int(changes.get("track_total", cur[1]) or 0)
            if n:
                tags["trkn"] = [(n, total)]
            else:
                tags.pop("trkn", None)
        if "disc" in changes:
            d = int(changes["disc"] or 0)
            if d:
                tags["disk"] = [(d, 0)]
            else:
                tags.pop("disk", None)
        if cover:
            fmt = MP4Cover.FORMAT_PNG if image_mime(cover) == "image/png" else MP4Cover.FORMAT_JPEG
            tags["covr"] = [MP4Cover(cover, imageformat=fmt)]
        f.save()
        return

    for k, cls in _ID3_TEXT.items():
        if k in changes:
            v = changes[k]
            fid = cls.__name__
            tags.delall(fid)
            if k == "year":
                tags.delall("TYER")
            if v not in (None, "", 0):
                tags.add(cls(encoding=3, text=[str(v)]))
    if "track" in changes or "track_total" in changes:
        cur = _pair(str(tags["TRCK"].text[0])) if tags.get("TRCK") else (0, 0)
        n = int(changes.get("track", cur[0]) or 0)
        total = int(changes.get("track_total", cur[1]) or 0)
        tags.delall("TRCK")
        if n:
            tags.add(TRCK(encoding=3, text=[f"{n}/{total}" if total else str(n)]))
    if "disc" in changes:
        tags.delall("TPOS")
        if changes["disc"]:
            tags.add(TPOS(encoding=3, text=[str(int(changes["disc"]))]))
    if "lyrics" in changes:
        tags.delall("USLT")
        if changes["lyrics"]:
            tags.add(USLT(encoding=3, lang="eng", desc="", text=changes["lyrics"]))
    if cover:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=image_mime(cover), type=3, desc="", data=cover))
    f.save(v2_version=3)


# --------------------------------------------------------------------------------------
# Backups: an exact copy of the tag block, restorable later
# --------------------------------------------------------------------------------------

def _ser(v: Any) -> Dict[str, Any]:
    if isinstance(v, MP4Cover):
        return {"t": "cover", "fmt": int(v.imageformat), "b64": base64.b64encode(bytes(v)).decode()}
    if isinstance(v, MP4FreeForm):
        return {"t": "freeform", "fmt": int(v.dataformat), "b64": base64.b64encode(bytes(v)).decode()}
    if isinstance(v, bytes):
        return {"t": "bytes", "b64": base64.b64encode(v).decode()}
    if isinstance(v, bool):
        return {"t": "bool", "v": v}
    if isinstance(v, int):
        return {"t": "int", "v": v}
    if isinstance(v, tuple):
        return {"t": "tuple", "v": list(v)}
    return {"t": "str", "v": str(v)}


def _de(d: Dict[str, Any]) -> Any:
    t = d["t"]
    if t == "cover":
        return MP4Cover(base64.b64decode(d["b64"]), imageformat=d["fmt"])
    if t == "freeform":
        return MP4FreeForm(base64.b64decode(d["b64"]), dataformat=d["fmt"])
    if t == "bytes":
        return base64.b64decode(d["b64"])
    if t == "tuple":
        return tuple(d["v"])
    return d["v"]


def snapshot(path: str, dest_base: str) -> str:
    """Save the complete tag block of ``path`` next to ``dest_base``. Returns the file written."""
    f = _open(path)
    if ext(path) in MP4_EXT:
        out = dest_base + ".mp4tags.json"
        items: Dict[str, Any] = {}
        if f.tags is not None:
            for key, val in f.tags.items():
                items[key] = {"list": [_ser(x) for x in val]} if isinstance(val, list) else {"one": _ser(val)}
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"present": f.tags is not None, "items": items}, fh)
        return out
    out = dest_base + ".id3"
    if f.tags is None:
        with open(out + ".none", "w") as fh:
            fh.write("no tags\n")
        return out + ".none"
    side = ID3()
    for frame in f.tags.values():
        side.add(frame)
    open(out, "wb").close()
    side.save(out, v1=0, v2_version=4)
    meta = {"version": list(getattr(f.tags, "version", (2, 4, 0)))}
    with open(out + ".json", "w") as fh:
        json.dump(meta, fh)
    return out


def restore(path: str, backup: str) -> None:
    """Put back the tag block saved by :func:`snapshot`."""
    f = _open(path)
    if backup.endswith(".mp4tags.json"):
        with open(backup, encoding="utf-8") as fh:
            data = json.load(fh)
        if f.tags is None:
            f.add_tags()
        f.tags.clear()
        for key, val in data["items"].items():
            f.tags[key] = [_de(x) for x in val["list"]] if "list" in val else _de(val["one"])
        f.save()
        return
    if backup.endswith(".none"):
        if f.tags is not None:
            f.tags.clear()
            f.save()
        return
    side = ID3(backup)
    version = 4
    try:
        with open(backup + ".json") as fh:
            version = int(json.load(fh)["version"][1]) or 4
    except (OSError, ValueError, KeyError, IndexError):
        pass
    if f.tags is None:
        f.add_tags()
    f.tags.clear()
    for frame in side.values():
        f.tags.add(frame)
    f.save(v2_version=3 if version == 3 else 4)


def cover_bytes(path: str) -> Optional[bytes]:
    f = _open(path)
    if f.tags is None:
        return None
    if ext(path) in MP4_EXT:
        c = f.tags.get("covr")
        return bytes(c[0]) if c else None
    pics = f.tags.getall("APIC")
    return pics[0].data if pics else None
