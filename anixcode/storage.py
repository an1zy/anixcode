"""Sessions and their messages in SQLite."""
from __future__ import annotations
from .i18n import tr

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Session:
    id: str
    cwd: str
    title: str = tr("Новая сессия")
    model: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    tokens_in: int = 0
    tokens_out: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    store: Store | None = field(default=None, repr=False, compare=False)

    def add(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        if self.title == tr("Новая сессия") and message.get("role") == "user":
            text = " ".join(str(message.get("content", "")).split())
            self.title = text[:60] + ("…" if len(text) > 60 else "")
        self.updated = time.time()
        if self.store:
            self.store.append(self, message)


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, cwd TEXT, title TEXT, model TEXT,
                    created REAL, updated REAL, tokens_in INT DEFAULT 0, tokens_out INT DEFAULT 0);
                CREATE TABLE IF NOT EXISTS messages (
                    session_id TEXT, idx INT, data TEXT, PRIMARY KEY (session_id, idx));
                CREATE TABLE IF NOT EXISTS snapshots (
                    session_id TEXT, turn INT, path TEXT, existed INT, content BLOB,
                    PRIMARY KEY (session_id, turn, path));
            """)

    def create(self, cwd: str, model: str = "") -> Session:
        s = Session(id=uuid.uuid4().hex, cwd=cwd, model=model, store=self)
        self._save_session(s)
        return s

    def _save_session(self, s: Session) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "title=excluded.title, model=excluded.model, updated=excluded.updated, "
                "tokens_in=excluded.tokens_in, tokens_out=excluded.tokens_out",
                (s.id, s.cwd, s.title, s.model, s.created, s.updated, s.tokens_in, s.tokens_out))

    def append(self, s: Session, message: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?)",
                             (s.id, len(s.messages) - 1, json.dumps(message, ensure_ascii=False)))
        self._save_session(s)

    def replace_messages(self, s: Session) -> None:
        """Rewrite the whole history (after compaction)."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM messages WHERE session_id = ?", (s.id,))
            self._db.executemany("INSERT INTO messages VALUES (?,?,?)",
                                 [(s.id, i, json.dumps(m, ensure_ascii=False)) for i, m in enumerate(s.messages)])
        self._save_session(s)

    # -- file snapshots for /undo ---------------------------------------------------
    def add_snapshot(self, session_id: str, turn: int, path: str, existed: bool, content: bytes) -> None:
        with self._lock, self._db:  # first snapshot of a file within a turn wins
            self._db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?)",
                             (session_id, turn, path, int(existed), content))

    def last_snapshot_turn(self, session_id: str) -> tuple[int, list[tuple[str, bool, bytes]]] | None:
        with self._lock:
            row = self._db.execute("SELECT MAX(turn) FROM snapshots WHERE session_id = ?", (session_id,)).fetchone()
            if not row or row[0] is None:
                return None
            files = self._db.execute("SELECT path, existed, content FROM snapshots WHERE session_id = ? AND turn = ?",
                                     (session_id, row[0])).fetchall()
        return row[0], [(p, bool(e), c) for p, e, c in files]

    def drop_snapshot_turn(self, session_id: str, turn: int) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM snapshots WHERE session_id = ? AND turn = ?", (session_id, turn))

    def touch(self, s: Session) -> None:
        self._save_session(s)

    def list(self, cwd: str | None = None, limit: int = 50) -> list[Session]:
        q = "SELECT id, cwd, title, model, created, updated, tokens_in, tokens_out FROM sessions"
        params: tuple[Any, ...] = ()
        if cwd:
            q += " WHERE cwd = ?"
            params = (cwd,)
        q += " ORDER BY updated DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(q, (*params, limit)).fetchall()
        return [Session(*row, store=self) for row in rows]

    def load(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._db.execute("SELECT id, cwd, title, model, created, updated, tokens_in, tokens_out "
                                   "FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not row:
                return None
            msgs = self._db.execute("SELECT data FROM messages WHERE session_id = ? ORDER BY idx",
                                    (session_id,)).fetchall()
        s = Session(*row, store=self)
        s.messages = [json.loads(m[0]) for m in msgs]
        return s

    def close(self) -> None:
        with self._lock:
            self._db.close()
