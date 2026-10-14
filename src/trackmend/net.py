"""HTTP with a persistent cache, polite pacing and retries (stdlib only)."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from . import __version__

USER_AGENT = f"trackmend/{__version__} (https://github.com/Bouliw/trackmend)"

# transport(url, headers) -> (HTTP status, body)
Transport = Callable[[str, Dict[str, str]], Tuple[int, bytes]]


@dataclass(frozen=True)
class HostRule:
    interval: float = 0.0       # seconds between two requests to this host
    backoff: float = 2.0        # first pause after an error
    step: float = 0.0           # pause grows by this much each time (0: it doubles instead)
    max_pause: float = 120.0
    retries: int = 6            # tries after the first one
    busy: Tuple[int, ...] = (429, 500, 502, 503, 504)  # answers meaning "try again later"

    def pause(self, attempt: int) -> float:
        grown = self.backoff + self.step * attempt if self.step else self.backoff * (2 ** attempt)
        return min(self.max_pause, grown)


HOSTS = {
    # 50 requests per 5 seconds allowed; quota errors come back as HTTP 200 (see deezer.py)
    "api.deezer.com": HostRule(interval=0.12),
    # about 20 requests per minute; answers 403 when asked too often
    "itunes.apple.com": HostRule(interval=3.1, backoff=60, max_pause=600, retries=5,
                                 busy=(403, 429, 500, 502, 503, 504)),
    # answers 503 at random when the server is overloaded (not a limit per address):
    # try again soon, 1.5 s, 2.5 s, 3.5 s... Long pauses only made searches far slower.
    "lrclib.net": HostRule(interval=0.05, backoff=1.5, step=1.0, max_pause=12, retries=10),
    # one request per second, 503 when asked faster
    "musicbrainz.org": HostRule(interval=1.1, backoff=3, max_pause=120),
    "coverartarchive.org": HostRule(interval=0.3),
}


def host_rule(url: str) -> HostRule:
    return HOSTS.get(urllib.parse.urlsplit(url).hostname or "", HostRule())


class NetError(Exception):
    pass


def urllib_transport(url: str, headers: Dict[str, str]) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""


def curl_transport(url: str, headers: Dict[str, str]) -> Tuple[int, bytes]:
    """Fallback for Python builds without CA certificates (common with python.org installers)."""
    cmd = ["curl", "-sSL", "--max-time", "30", "-w", "\n%{http_code}"]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    r = subprocess.run(cmd + [url], capture_output=True)
    if r.returncode != 0:
        raise OSError(r.stderr.decode(errors="replace").strip() or "curl failed")
    body, _, code = r.stdout.rpartition(b"\n")
    return int(code or 0), body


class Http:
    """GET with a persistent cache, per-host pacing and growing pauses on errors."""

    def __init__(self, cache_path: Optional[str], transport: Optional[Transport] = None,
                 ttl_days: float = 60, sleep: Callable[[float], None] = time.sleep,
                 log: Optional[Callable[[str], None]] = None):
        self.transport = transport or urllib_transport
        self.ttl = ttl_days * 86400
        self.sleep = sleep
        self.log = log
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._pace = threading.Lock()
        self.db = None
        if cache_path:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            self.db = sqlite3.connect(cache_path, timeout=30, check_same_thread=False)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("CREATE TABLE IF NOT EXISTS http (url TEXT PRIMARY KEY, status INTEGER,"
                            " body BLOB, time REAL)")
            self.db.commit()

    # -- cache -------------------------------------------------------------------------
    def _cached(self, url: str) -> Optional[Tuple[int, bytes]]:
        if self.db is None:
            return None
        with self._lock:
            row = self.db.execute("SELECT status, body, time FROM http WHERE url=?", (url,)).fetchone()
        if row and time.time() - row[2] < self.ttl:
            return row[0], row[1]
        return None

    def _store(self, url: str, status: int, body: bytes) -> None:
        if self.db is None:
            return
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO http VALUES (?, ?, ?, ?)", (url, status, body, time.time()))
            self.db.commit()

    # -- network -----------------------------------------------------------------------
    def _fetch(self, url: str, headers: Dict[str, str]) -> Tuple[int, bytes]:
        host = urllib.parse.urlsplit(url).hostname or ""
        rule = host_rule(url)
        with self._pace:
            wait = rule.interval - (time.monotonic() - self._last.get(host, -1e9))
            if wait > 0:
                self.sleep(wait)
            self._last[host] = time.monotonic()
        try:
            return self.transport(url, headers)
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), ssl.SSLError) and self.transport is urllib_transport:
                self.transport = curl_transport
                return self.transport(url, headers)
            raise

    def _pause(self, rule: HostRule, attempt: int, why: str, url: str) -> None:
        if attempt >= rule.retries:
            return  # that was the last try: no point waiting
        pause = rule.pause(attempt)
        if self.log and pause >= 30:
            host = urllib.parse.urlsplit(url).hostname
            self.log(f"  {host} {why}; waiting {int(pause)} s before trying again")
        self.sleep(pause)

    def get(self, url: str, headers: Optional[Dict[str, str]] = None,
            retry_if: Optional[Callable[[int, bytes], bool]] = None) -> Tuple[int, bytes]:
        """Returns (status, body). 200 and 404 answers are cached; busy answers are retried."""
        hit = self._cached(url)
        if hit:
            return hit
        rule = host_rule(url)
        h = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
        last_error = ""
        for attempt in range(rule.retries + 1):
            try:
                status, body = self._fetch(url, h)
            except (OSError, ValueError) as e:
                last_error = str(e)
                self._pause(rule, attempt, "cannot be reached", url)
                continue
            if retry_if and retry_if(status, body):
                last_error = "rate limited"
                self._pause(rule, attempt, "asks to slow down", url)
                continue
            if status in (200, 404):
                self._store(url, status, body)
                return status, body
            last_error = f"HTTP {status}"
            if status in rule.busy:
                self._pause(rule, attempt, f"is busy (HTTP {status})", url)
                continue
            return status, body
        raise NetError(f"{url.split('?')[0]}: {last_error}")

    def get_json(self, url: str, **kw) -> Optional[object]:
        status, body = self.get(url, **kw)
        if status != 200:
            return None
        try:
            return json.loads(body)
        except ValueError:
            return None

    def memo(self, key: str, compute: Callable[[], object]) -> object:
        """Cache the JSON-able result of something that is not an HTTP request (a yt-dlp search)."""
        hit = self._cached(key)
        if hit:
            return json.loads(hit[1])
        value = compute()
        self._store(key, 200, json.dumps(value).encode())
        return value

    def download(self, url: str, folder: str, min_size: int = 2000) -> Optional[bytes]:
        """Download a file once (artwork). Returns its bytes, or None."""
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, hashlib.sha1(url.encode()).hexdigest())
        if os.path.exists(path):
            with open(path, "rb") as fh:
                return fh.read()
        try:
            status, body = self._fetch(url, {"User-Agent": USER_AGENT})
        except OSError:
            return None
        if status != 200 or len(body) < min_size:
            return None
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(body)
        os.replace(tmp, path)
        return body
