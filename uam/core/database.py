"""SQLite storage: inventory cache, review classifications, history, settings."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
from typing import Any, Iterable

from .application_model import AppRecord, Classification, UpdateInfo

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    key         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    data        TEXT NOT NULL,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS classifications (
    key         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('application', 'component', 'ignore')),
    name        TEXT,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL,
    source      TEXT,
    status      TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS history_ts ON history (ts DESC);
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS updates (
    key         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    data        TEXT NOT NULL,
    checked_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS github_links (
    link_key    TEXT PRIMARY KEY,
    repo        TEXT NOT NULL,          -- owner/name, or '' when the user turned tracking off
    origin      TEXT NOT NULL,          -- 'user' or 'none'
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS github_cache (
    repo        TEXT PRIMARY KEY,
    etag        TEXT,
    data        TEXT NOT NULL,          -- trimmed release list (JSON)
    checked_at  REAL NOT NULL
);
"""

HISTORY_DETAIL_MAX = 300


def default_db_path() -> str:
    from .. import APP_SLUG, LEGACY_SLUG

    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    current = os.path.join(base, APP_SLUG)
    legacy = os.path.join(base, LEGACY_SLUG)
    # Keep inventory, reviews, history and settings across the rename.
    if not os.path.exists(current) and os.path.isdir(legacy) and not os.path.islink(legacy):
        try:
            os.rename(legacy, current)
        except OSError:
            return os.path.join(legacy, "inventory.db")
    return os.path.join(current, "inventory.db")


def sanitize_detail(text: str | None) -> str:
    """History never stores raw command output; keep a short single-line summary."""
    if not text:
        return ""
    line = " ".join(str(text).split())
    return line[:HISTORY_DETAIL_MAX]


class Database:
    def __init__(self, path: str | None = None):
        self.path = path or default_db_path()
        self.memory = self.path == ":memory:"
        if not self.memory:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        # File databases get one connection per thread: scans persist their results
        # from a worker thread while the UI keeps reading (WAL mode). An in-memory
        # database exists only in its connection, so it is shared under a lock.
        self._lock = threading.RLock()
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._conn = self._connect()
        with self._lock, self._conn:
            if not self.memory:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        if not self.memory:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        if not self.memory:
            conn.execute("PRAGMA synchronous=NORMAL")
        self._all.append(conn)
        self._local.conn = conn
        return conn

    def _guard(self):
        return self._lock if self.memory else contextlib.nullcontext()

    @property
    def conn(self) -> sqlite3.Connection:
        if self.memory:
            return self._conn
        conn = getattr(self._local, "conn", None)
        return conn if conn is not None else self._connect()

    def close(self) -> None:
        with self._lock:
            for conn in self._all:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._all.clear()

    # -- inventory ------------------------------------------------------------
    def load_inventory(self) -> list[AppRecord]:
        with self._guard():
            rows = self.conn.execute("SELECT data, first_seen FROM apps").fetchall()
        records = []
        for row in rows:
            try:
                record = AppRecord.from_dict(json.loads(row["data"]))
            except (ValueError, KeyError, TypeError):
                continue
            record.first_seen = row["first_seen"]
            records.append(record)
        return records

    def save_inventory(self, records: Iterable[AppRecord]) -> tuple[set[str], set[str]]:
        """Replace the stored inventory. Returns (new_keys, removed_keys)."""
        now = time.time()
        records = list(records)
        with self._guard(), self.conn:
            existing = {r["key"]: r["first_seen"] for r in self.conn.execute("SELECT key, first_seen FROM apps")}
            current = {r.key for r in records}
            for record in records:
                record.first_seen = existing.get(record.key, now)
                self.conn.execute(
                    "INSERT INTO apps (key, source, data, first_seen, last_seen) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET source=excluded.source, data=excluded.data, "
                    "last_seen=excluded.last_seen",
                    (record.key, record.source.value, json.dumps(record.to_dict()), record.first_seen, now),
                )
            removed = set(existing) - current
            self.conn.executemany("DELETE FROM apps WHERE key = ?", [(k,) for k in removed])
        return current - set(existing), removed

    def inventory_is_empty(self) -> bool:
        with self._guard():
            return self.conn.execute("SELECT COUNT(*) FROM apps").fetchone()[0] == 0

    # -- classifications --------------------------------------------------------
    def classifications(self) -> dict[str, Classification]:
        with self._guard():
            rows = self.conn.execute("SELECT key, kind FROM classifications").fetchall()
        return {row["key"]: Classification(row["kind"]) for row in rows}

    def set_classification(self, key: str, kind: Classification | None, name: str = "") -> None:
        with self._guard(), self.conn:
            if kind is None:
                self.conn.execute("DELETE FROM classifications WHERE key = ?", (key,))
            else:
                self.conn.execute(
                    "INSERT INTO classifications (key, kind, name, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET kind=excluded.kind, name=excluded.name, "
                    "updated_at=excluded.updated_at",
                    (key, kind.value, name, time.time()),
                )

    # -- history ------------------------------------------------------------------
    def add_history(self, action: str, target: str, status: str, source: str = "", detail: str = "") -> None:
        with self._guard(), self.conn:
            self.conn.execute(
                "INSERT INTO history (ts, action, target, source, status, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), action, sanitize_detail(target), source, status, sanitize_detail(detail)),
            )
            # Keep the log bounded.
            self.conn.execute(
                "DELETE FROM history WHERE id IN (SELECT id FROM history ORDER BY ts DESC LIMIT -1 OFFSET 5000)")

    def history(self, limit: int = 500, action: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM history"
        args: list[Any] = []
        if action:
            query += " WHERE action = ?"
            args.append(action)
        query += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        with self._guard():
            return [dict(row) for row in self.conn.execute(query, args).fetchall()]

    def clear_history(self) -> None:
        with self._guard(), self.conn:
            self.conn.execute("DELETE FROM history")

    # -- settings -----------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._guard():
            row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        with self._guard(), self.conn:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    # -- updates cache ------------------------------------------------------------
    def save_updates(self, source: str, updates: list[UpdateInfo]) -> None:
        now = time.time()
        with self._guard(), self.conn:
            self.conn.execute("DELETE FROM updates WHERE source = ?", (source,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO updates (key, source, data, checked_at) VALUES (?, ?, ?, ?)",
                [(u.key, source, json.dumps(u.to_dict()), now) for u in updates],
            )
            self._set_meta(f"updates_checked:{source}", str(now))

    def load_updates(self) -> list[UpdateInfo]:
        with self._guard():
            rows = self.conn.execute("SELECT data FROM updates").fetchall()
        result = []
        for row in rows:
            try:
                result.append(UpdateInfo.from_dict(json.loads(row["data"])))
            except (ValueError, TypeError):
                continue
        return result

    def remove_update(self, key: str) -> None:
        with self._guard(), self.conn:
            self.conn.execute("DELETE FROM updates WHERE key = ?", (key,))

    def updates_checked_at(self, source: str) -> float | None:
        value = self.get_meta(f"updates_checked:{source}")
        return float(value) if value else None

    # -- GitHub release tracking ----------------------------------------------------
    def github_links(self) -> dict[str, tuple[str, str]]:
        with self._guard():
            rows = self.conn.execute("SELECT link_key, repo, origin FROM github_links").fetchall()
        return {row["link_key"]: (row["repo"], row["origin"]) for row in rows}

    def set_github_link(self, link_key: str, repo: str | None, origin: str = "user") -> None:
        """Store a user decision. ``repo=None`` forgets it (auto-detection applies again)."""
        with self._guard(), self.conn:
            if repo is None:
                self.conn.execute("DELETE FROM github_links WHERE link_key = ?", (link_key,))
            else:
                self.conn.execute(
                    "INSERT OR REPLACE INTO github_links (link_key, repo, origin, updated_at) VALUES (?, ?, ?, ?)",
                    (link_key, repo, origin, time.time()))

    def github_cache(self, repo: str) -> tuple[str | None, Any, float] | None:
        with self._guard():
            row = self.conn.execute("SELECT etag, data, checked_at FROM github_cache WHERE repo = ?",
                                    (repo,)).fetchone()
        if row is None:
            return None
        try:
            return row["etag"], json.loads(row["data"]), row["checked_at"]
        except ValueError:
            return None

    def set_github_cache(self, repo: str, etag: str | None, data: Any) -> None:
        with self._guard(), self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO github_cache (repo, etag, data, checked_at) VALUES (?, ?, ?, ?)",
                (repo, etag, json.dumps(data), time.time()))

    # -- meta ---------------------------------------------------------------------
    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def set_meta(self, key: str, value: str) -> None:
        with self._guard(), self.conn:
            self._set_meta(key, value)

    def get_meta(self, key: str) -> str | None:
        with self._guard():
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    # -- backup -------------------------------------------------------------------
    def backup(self, destination: str) -> None:
        """Write a consistent copy of the database using SQLite's backup API."""
        if os.path.abspath(destination) == os.path.abspath(self.path):
            raise ValueError("Backup destination is the live database")
        tmp = destination + ".partial"
        target = sqlite3.connect(tmp)
        try:
            with self._guard():
                self.conn.backup(target)
        finally:
            target.close()
        os.replace(tmp, destination)
