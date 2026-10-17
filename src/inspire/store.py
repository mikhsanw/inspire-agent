"""SQLite store: sesi, riwayat pesan, dan pencarian FTS5."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY,
    chat_id     TEXT NOT NULL,
    started_at  REAL NOT NULL,
    last_at     REAL NOT NULL,
    summary     TEXT,              -- ringkasan pesan sebelum context_from
    context_from INTEGER DEFAULT 0 -- id pesan pertama yang masih masuk konteks
);
CREATE INDEX IF NOT EXISTS idx_sessions_chat ON sessions(chat_id, id);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER NOT NULL REFERENCES sessions(id),
    chat_id      TEXT NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT,
    tool_calls   TEXT,             -- JSON, untuk role=assistant
    tool_call_id TEXT,             -- untuk role=tool
    name         TEXT,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, content='messages', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages
WHEN new.role IN ('user', 'assistant') AND new.content IS NOT NULL BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


@dataclass
class Session:
    id: int
    chat_id: str
    started_at: float
    last_at: float
    summary: str | None
    context_from: int


class Store:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- sesi -----------------------------------------------------------

    def current_session(self, chat_id: str) -> Session | None:
        row = self.db.execute(
            "SELECT * FROM sessions WHERE chat_id=? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return Session(**dict(row)) if row else None

    def new_session(self, chat_id: str) -> Session:
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO sessions(chat_id, started_at, last_at) VALUES (?,?,?)",
            (chat_id, now, now),
        )
        self.db.commit()
        return Session(cur.lastrowid, chat_id, now, now, None, 0)

    def touch(self, session_id: int) -> None:
        self.db.execute(
            "UPDATE sessions SET last_at=? WHERE id=?", (time.time(), session_id)
        )
        self.db.commit()

    def set_summary(self, session_id: int, summary: str, context_from: int) -> None:
        self.db.execute(
            "UPDATE sessions SET summary=?, context_from=? WHERE id=?",
            (summary, context_from, session_id),
        )
        self.db.commit()

    # --- pesan ----------------------------------------------------------

    def add_message(self, session_id: int, chat_id: str, msg: dict) -> int:
        tool_calls = msg.get("tool_calls")
        cur = self.db.execute(
            """INSERT INTO messages(session_id, chat_id, role, content, tool_calls,
               tool_call_id, name, created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (
                session_id,
                chat_id,
                msg["role"],
                msg.get("content"),
                json.dumps(tool_calls) if tool_calls else None,
                msg.get("tool_call_id"),
                msg.get("name"),
                time.time(),
            ),
        )
        self.db.commit()
        return cur.lastrowid

    def context_messages(self, session: Session) -> list[tuple[int, dict]]:
        """Pesan sesi yang masih masuk konteks, format OpenAI, dengan id-nya."""
        rows = self.db.execute(
            "SELECT * FROM messages WHERE session_id=? AND id>=? ORDER BY id",
            (session.id, session.context_from),
        ).fetchall()
        return [(r["id"], _row_to_msg(r)) for r in rows]

    def search(self, query: str, chat_id: str, limit: int = 8) -> list[dict]:
        rows = self.db.execute(
            """SELECT m.id, m.session_id, m.role, m.created_at,
                      snippet(messages_fts, 0, '[', ']', '…', 24) AS snip
               FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid
               WHERE messages_fts MATCH ? AND m.chat_id=?
               ORDER BY rank LIMIT ?""",
            (_fts_query(query), chat_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def _row_to_msg(r: sqlite3.Row) -> dict:
    msg: dict = {"role": r["role"], "content": r["content"]}
    if r["tool_calls"]:
        msg["tool_calls"] = json.loads(r["tool_calls"])
    if r["tool_call_id"]:
        msg["tool_call_id"] = r["tool_call_id"]
    if r["name"]:
        msg["name"] = r["name"]
    return msg


def _fts_query(text: str) -> str:
    """Ubah teks bebas jadi query FTS5 aman: tiap kata di-quote, digabung OR."""
    words = [w.replace('"', "") for w in text.split()]
    return " OR ".join(f'"{w}"' for w in words if w) or '""'
