"""Everything trackmend keeps on disk: lock, progress, run journals, backups, cache.

Layout (``~/Library/Application Support/trackmend`` unless ``TRACKMEND_HOME`` is set)::

    lock                      held by the running command (flock, released on exit)
    state.sqlite              which tracks each command already handled (for resuming)
    cache.sqlite              HTTP answers from Deezer and LRCLIB
    covers/                   downloaded artwork
    runs/<id>/journal.jsonl   every change, written before and after it happens
    runs/<id>/backup/         original tag blocks
    runs/<id>/removed/        files of tracks taken out of Music (never deleted)
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
import sys
import time
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore


def default_home() -> str:
    env = os.environ.get("TRACKMEND_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/trackmend")
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "trackmend")


class Busy(Exception):
    """Another trackmend command is already working on the library."""


class Lock:
    """Exclusive lock on the library. The OS releases it if the process dies."""

    def __init__(self, path: str, command: str):
        self.path, self.command, self.fh = path, command, None

    def __enter__(self) -> "Lock":
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fh = open(self.path, "a+")
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                fh.seek(0)
                holder = fh.read().strip() or "another process"
                fh.close()
                raise Busy(holder) from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"trackmend {self.command} (pid {os.getpid()}, since {time.strftime('%Y-%m-%d %H:%M:%S')})\n")
        fh.flush()
        self.fh = fh
        return self

    def __exit__(self, *exc) -> None:
        if self.fh:
            self.fh.seek(0)
            self.fh.truncate()
            if fcntl is not None:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            self.fh.close()
            self.fh = None


class Progress:
    """Per-command record of handled tracks, so an interrupted run picks up where it stopped."""

    def __init__(self, path: str, command: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS progress (command TEXT, pid TEXT, outcome TEXT, detail TEXT,"
            " synced INTEGER DEFAULT 1, updated REAL, PRIMARY KEY (command, pid))"
        )
        self.db.commit()
        self.command = command

    def get(self) -> Dict[str, str]:
        rows = self.db.execute("SELECT pid, outcome FROM progress WHERE command=?", (self.command,))
        return dict(rows.fetchall())

    def entries(self) -> Dict[str, Tuple[str, str]]:
        """pid -> (outcome, detail)."""
        rows = self.db.execute("SELECT pid, outcome, detail FROM progress WHERE command=?", (self.command,))
        return {pid: (outcome, detail or "") for pid, outcome, detail in rows.fetchall()}

    def set(self, pid: str, outcome: str, detail: str = "", synced: bool = True) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO progress VALUES (?, ?, ?, ?, ?, ?)",
            (self.command, pid, outcome, detail, 1 if synced else 0, time.time()),
        )
        self.db.commit()

    def unsynced(self) -> List[str]:
        rows = self.db.execute("SELECT pid FROM progress WHERE command=? AND synced=0", (self.command,))
        return [r[0] for r in rows.fetchall()]

    def mark_synced(self, pids: Iterable[str]) -> None:
        self.db.executemany("UPDATE progress SET synced=1 WHERE command=? AND pid=?",
                            [(self.command, p) for p in pids])
        self.db.commit()

    def forget(self, pids: Iterable[str]) -> None:
        self.db.executemany("DELETE FROM progress WHERE command=? AND pid=?",
                            [(self.command, p) for p in pids])
        self.db.commit()

    def close(self) -> None:
        self.db.close()


class Run:
    """One command that changed something. Its journal is append-only and fsynced."""

    def __init__(self, path: str):
        self.path = path
        self.journal_path = os.path.join(path, "journal.jsonl")
        self._seq = 0
        if os.path.exists(self.journal_path):
            for e in self.entries(raw=True):
                self._seq = max(self._seq, int(e.get("seq", 0)))

    @property
    def id(self) -> str:
        return os.path.basename(self.path)

    @classmethod
    def create(cls, runs_dir: str, command: str, argv: List[str]) -> "Run":
        os.makedirs(runs_dir, exist_ok=True)
        base = time.strftime("%Y%m%d-%H%M%S") + "-" + command
        path, n = os.path.join(runs_dir, base), 1
        while os.path.exists(path):
            n += 1
            path = os.path.join(runs_dir, f"{base}-{n}")
        os.makedirs(os.path.join(path, "backup"))
        with open(os.path.join(path, "meta.json"), "w") as fh:
            json.dump({"command": command, "argv": argv, "started": time.time()}, fh)
        return cls(path)

    def _append(self, entry: Dict) -> None:
        with open(self.journal_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def begin(self, op: str, **data) -> int:
        """Record a change about to happen. Returns its sequence number."""
        self._seq += 1
        self._append({"seq": self._seq, "op": op, "status": "pending", "time": time.time(), **data})
        return self._seq

    def note(self, seq: int, **data) -> None:
        self._append({"seq": seq, **data})

    def done(self, seq: int, **data) -> None:
        self._append({"seq": seq, "status": "done", **data})

    def mark_undone(self, seq: int) -> None:
        self._append({"seq": seq, "status": "undone"})

    def backup_base(self, seq: int) -> str:
        return os.path.join(self.path, "backup", f"{seq:05d}")

    def removed_path(self, seq: int, original: str) -> str:
        d = os.path.join(self.path, "removed")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{seq:05d}-{os.path.basename(original)}")

    def entries(self, raw: bool = False) -> List[Dict]:
        """Journal entries merged by sequence number, in order."""
        if not os.path.exists(self.journal_path):
            return []
        lines: List[Dict] = []
        with open(self.journal_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.append(json.loads(line))
                except ValueError:
                    continue  # a line cut by a crash
        if raw:
            return lines
        merged: Dict[int, Dict] = {}
        for e in lines:
            merged.setdefault(int(e["seq"]), {}).update(e)
        return [merged[k] for k in sorted(merged)]

    def meta(self) -> Dict:
        try:
            with open(os.path.join(self.path, "meta.json")) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}


class Home:
    def __init__(self, root: Optional[str] = None):
        self.root = root or default_home()
        self.runs_dir = os.path.join(self.root, "runs")
        self.covers_dir = os.path.join(self.root, "covers")
        self.cache_db = os.path.join(self.root, "cache.sqlite")
        self.state_db = os.path.join(self.root, "state.sqlite")
        self.lock_path = os.path.join(self.root, "lock")

    def lock(self, command: str) -> Lock:
        return Lock(self.lock_path, command)

    def progress(self, command: str) -> Progress:
        return Progress(self.state_db, command)

    def new_run(self, command: str, argv: List[str]) -> Run:
        return Run.create(self.runs_dir, command, argv)

    def runs(self) -> List[Run]:
        if not os.path.isdir(self.runs_dir):
            return []
        names = sorted(n for n in os.listdir(self.runs_dir) if os.path.isdir(os.path.join(self.runs_dir, n)))
        return [Run(os.path.join(self.runs_dir, n)) for n in names]

    def run(self, run_id: str) -> Optional[Run]:
        for r in self.runs():
            if r.id == run_id or r.id.startswith(run_id):
                return r
        return None


def move(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
