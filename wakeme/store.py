"""plugin_db(): watches, transcript rows, summaries, events, pending alerts. Short writes, one lock."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

PLUGIN = "wake-me-when"
LIVE_STATUSES = ("planning", "watching", "behind", "fired")


def _start_time(pid: int) -> str:
    """Process start time (Linux /proc), so a recycled pid never looks like the old owner."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


OWNER_TOKEN = f"{os.getpid()}:{_start_time(os.getpid()) or uuid.uuid4().hex[:8]}"


def owner_alive(token: str) -> bool:
    try:
        pid_s, start = (token or "").split(":", 1)
        pid = int(pid_s)
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    st = _start_time(pid)
    return not st or not start or st == start

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, title TEXT, kind TEXT, condition TEXT,
  plan TEXT, mode TEXT, status TEXT, note TEXT, session_key TEXT, session_id TEXT, platform TEXT,
  created REAL, ended REAL, owner TEXT, alerts INTEGER DEFAULT 0, workdir TEXT);
CREATE TABLE IF NOT EXISTS rows (
  id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id INTEGER, pos REAL, pos_end REAL, wall REAL, text TEXT);
CREATE INDEX IF NOT EXISTS rows_watch ON rows(watch_id, id);
CREATE TABLE IF NOT EXISTS summaries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id INTEGER, pos REAL, pos_end REAL, wall REAL, text TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id INTEGER, kind TEXT, wall REAL, pos REAL, data TEXT);
CREATE TABLE IF NOT EXISTS pending (
  id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id INTEGER, session_key TEXT, wall REAL, text TEXT,
  delivered INTEGER DEFAULT 0);
"""


def data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir(PLUGIN)
    except ImportError:  # tests / outside Hermes
        root = Path(os.environ.get("WAKEME_DATA_DIR") or Path.home() / ".wake-me-when-dev")
        root.mkdir(parents=True, exist_ok=True)
        return root


def _connect() -> sqlite3.Connection:
    try:
        from plugins.plugin_storage import plugin_db
        conn = plugin_db(PLUGIN)
    except ImportError:
        conn = sqlite3.connect(data_dir() / "data.db", check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class Store:
    def __init__(self, conn: Optional[sqlite3.Connection] = None):
        self.conn = conn or _connect()
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(_SCHEMA)
            self.conn.commit()

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    # ---- watches ----------------------------------------------------------------------------

    def create_watch(self, url: str, kind: str, condition: str, mode: str, session_key: str, session_id: str,
                     platform: str) -> int:
        cur = self._x("INSERT INTO watches (url, kind, condition, mode, status, session_key, session_id, platform,"
                      " created, owner) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (url, kind, condition, mode, "planning", session_key, session_id, platform, time.time(),
                       OWNER_TOKEN))
        return int(cur.lastrowid)

    def update_watch(self, wid: int, **fields: Any) -> None:
        if not fields:
            return
        if "plan" in fields and not isinstance(fields["plan"], str):
            fields["plan"] = json.dumps(fields["plan"])
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE watches SET {cols} WHERE id=?", (*fields.values(), wid))

    def watch(self, wid: int) -> Optional[dict]:
        rows = self._q("SELECT * FROM watches WHERE id=?", (wid,))
        return self._as_dict(rows[0]) if rows else None

    def watches(self, live_only: bool = False, limit: int = 20) -> list[dict]:
        if live_only:
            q = f"SELECT * FROM watches WHERE status IN ({','.join('?' * len(LIVE_STATUSES))}) ORDER BY id DESC"
            rows = self._q(q, LIVE_STATUSES)
        else:
            rows = self._q("SELECT * FROM watches ORDER BY id DESC LIMIT ?", (limit,))
        return [self._as_dict(r) for r in rows]

    def count_live(self) -> int:
        q = f"SELECT COUNT(*) FROM watches WHERE status IN ({','.join('?' * len(LIVE_STATUSES))})"
        return int(self._q(q, LIVE_STATUSES)[0][0])

    def _as_dict(self, row: tuple) -> dict:
        with self.lock:
            cols = [d[1] for d in self.conn.execute("PRAGMA table_info(watches)").fetchall()]
        d = dict(zip(cols, row))
        try:
            d["plan"] = json.loads(d["plan"]) if d.get("plan") else None
        except (TypeError, ValueError):
            d["plan"] = None
        return d

    def mark_orphans(self) -> int:
        """On load: a live row whose owning process is gone is marked stopped, so /watching never lies
        (resume is not implemented). Rows of a live process (e.g. the gateway, seen from a CLI
        subcommand) are left alone."""
        dead = [w["id"] for w in self.watches(live_only=True) if w.get("owner") != OWNER_TOKEN
                and not owner_alive(w.get("owner") or "")]
        for wid in dead:
            self.update_watch(wid, status="stopped", note="gateway restart", ended=time.time())
        return len(dead)

    # ---- transcript ---------------------------------------------------------------------------

    def add_row(self, wid: int, pos: float, pos_end: float, wall: float, text: str) -> int:
        return int(self._x("INSERT INTO rows (watch_id, pos, pos_end, wall, text) VALUES (?,?,?,?,?)",
                           (wid, pos, pos_end, wall, text)).lastrowid)

    def rows(self, wid: int, since_pos: float = -1, after_id: int = 0, limit: int = 2000) -> list[tuple]:
        """(id, pos, pos_end, wall, text) oldest first."""
        return self._q("SELECT id, pos, pos_end, wall, text FROM rows WHERE watch_id=? AND pos>=? AND id>? "
                       "ORDER BY id LIMIT ?", (wid, since_pos, after_id, limit))

    def rows_by_id(self, ids: list[int]) -> list[tuple]:
        if not ids:
            return []
        return self._q(f"SELECT id, pos, pos_end, wall, text FROM rows WHERE id IN ({','.join('?' * len(ids))}) "
                       "ORDER BY id", tuple(ids))

    def add_summary(self, wid: int, pos: float, pos_end: float, text: str) -> None:
        self._x("INSERT INTO summaries (watch_id, pos, pos_end, wall, text) VALUES (?,?,?,?,?)",
                (wid, pos, pos_end, time.time(), text))

    def summaries(self, wid: int) -> list[tuple]:
        return self._q("SELECT pos, pos_end, text FROM summaries WHERE watch_id=? ORDER BY pos", (wid,))

    # ---- events / pending ---------------------------------------------------------------------

    def event(self, wid: int, kind: str, pos: float = 0.0, **data: Any) -> int:
        return int(self._x("INSERT INTO events (watch_id, kind, wall, pos, data) VALUES (?,?,?,?,?)",
                           (wid, kind, time.time(), pos, json.dumps(data))).lastrowid)

    def events(self, wid: int, kind: Optional[str] = None) -> list[dict]:
        q = "SELECT id, kind, wall, pos, data FROM events WHERE watch_id=?" + (" AND kind=?" if kind else "")
        rows = self._q(q + " ORDER BY id", (wid, kind) if kind else (wid,))
        return [{"id": r[0], "kind": r[1], "wall": r[2], "pos": r[3], **json.loads(r[4] or "{}")} for r in rows]

    def queue_pending(self, wid: int, session_key: str, text: str) -> None:
        self._x("INSERT INTO pending (watch_id, session_key, wall, text) VALUES (?,?,?,?)",
                (wid, session_key, time.time(), text))

    def take_pending(self, session_key: str = "") -> list[tuple]:
        q = "SELECT id, watch_id, wall, text FROM pending WHERE delivered=0" + (" AND session_key=?" if session_key else "")
        rows = self._q(q + " ORDER BY id", (session_key,) if session_key else ())
        if rows:
            self._x(f"UPDATE pending SET delivered=1 WHERE id IN ({','.join('?' * len(rows))})",
                    tuple(r[0] for r in rows))
        return rows

    def prune(self, transcript_days: float) -> None:
        cutoff = time.time() - transcript_days * 86400
        old = [r[0] for r in self._q("SELECT id FROM watches WHERE ended IS NOT NULL AND ended < ?", (cutoff,))]
        for table in ("rows", "summaries", "events", "pending"):
            self._x(f"DELETE FROM {table} WHERE wall < ?", (cutoff,))
        if old:
            self._x(f"DELETE FROM watches WHERE id IN ({','.join('?' * len(old))})", tuple(old))
