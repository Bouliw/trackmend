from __future__ import annotations

import io
import os
import shutil
import struct
import subprocess
import sys
import zlib

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from fakes import FakeHttp, FakeMusic  # noqa: E402
from trackmend import audio  # noqa: E402
from trackmend import cli  # noqa: E402
from trackmend.net import Http  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")

_CODECS = {
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "64k"],
    ".m4a": ["-c:a", "aac", "-b:a", "64k"],
    ".aiff": ["-c:a", "pcm_s16be"],
}


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", *args], check=True)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No waiting for running processes (macOS "--write-via auto") and no real yt-dlp search."""
    from trackmend import tags, youtube
    monkeypatch.setattr(tags, "SAMPLES", 0)
    monkeypatch.setattr(youtube, "search", lambda query, limit=8: [])


@pytest.fixture(scope="session")
def tone_cache(tmp_path_factory):
    """One generated file per (format, seconds), copied for each test."""
    root = tmp_path_factory.mktemp("tones")
    made = {}

    def get(ext: str, seconds: float) -> str:
        key = (ext, seconds)
        if key not in made:
            path = str(root / f"tone-{seconds:g}{ext}")
            if seconds > 120 and ext == ".mp3":  # long files: silence at a low rate is much faster
                ffmpeg("-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", str(seconds),
                       "-c:a", "libmp3lame", "-b:a", "8k", path)
            else:
                ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-ac", "1",
                       *_CODECS[ext], path)
            made[key] = path
        return made[key]

    return get


def png_bytes(color=(200, 30, 30), size=8) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(color) * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture
def make_audio(tmp_path, tone_cache):
    """make_audio("Artist - Title.mp3", seconds=3, title=..., artist=..., cover=True)."""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg is not installed")
    media = tmp_path / "Music Media"
    media.mkdir(exist_ok=True)

    def make(name: str, seconds: float = 3.0, folder=None, cover: bool = False, **tags) -> str:
        ext = os.path.splitext(name)[1].lower()
        d = folder or media
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        shutil.copyfile(tone_cache(ext, seconds), path)
        if tags or cover:
            audio.write(path, tags, png_bytes() if cover else None)
        return path

    return make


@pytest.fixture
def media(tmp_path):
    d = tmp_path / "Music Media"
    d.mkdir(exist_ok=True)
    return str(d)


@pytest.fixture
def music(media):
    return FakeMusic(media)


@pytest.fixture
def home(tmp_path):
    return str(tmp_path / "home")


@pytest.fixture
def web():
    return FakeHttp()


@pytest.fixture
def run_cli(music, home, web):
    """run_cli("tags", "--apply") -> (exit code, output)."""

    def run(*argv, interactive=False, transport=None, **kw):
        out = io.StringIO()
        t = transport or web
        code = cli.main(["--home", home, *argv], music=kw.get("library", music),
                        http_factory=lambda: Http(os.path.join(home, "cache.sqlite"), transport=t,
                                                  sleep=lambda s: None),
                        out=out, interactive=interactive)
        return code, out.getvalue()

    return run
