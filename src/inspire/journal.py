"""Jurnal kerja agent: setiap aksi yang mengubah sesuatu dicatat, dan bila bisa,
dibatalkan dengan /rollback <nomor>.

- server: snapshot git/file dari tool host_* (lihat tools/host.py)
- sandbox: isi file sebelum ditulis/diubah disimpan di sini (maks 1MB)
- perintah lain (migrate, pip install, reload layanan, SELECT): dicatat saja
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    id         INTEGER PRIMARY KEY,
    ts         REAL NOT NULL,
    chat_id    TEXT NOT NULL,
    place      TEXT NOT NULL,      -- 'sandbox' | 'server' | 'db'
    site       TEXT DEFAULT '',
    tool       TEXT NOT NULL,
    summary    TEXT NOT NULL,
    snapshot   TEXT DEFAULT '',    -- server: git:<ts> / file:<ts>
    path       TEXT DEFAULT '',    -- sandbox: file yang diubah
    backup     BLOB,               -- sandbox: isi sebelum diubah
    created    INTEGER DEFAULT 0,  -- sandbox: file baru (rollback = hapus)
    status     TEXT DEFAULT 'ok',  -- ok | exit N | dibatalkan
    undone_by  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal(id DESC);
"""
MAX_BACKUP = 1024 * 1024
ICON = {"sandbox": "🧪", "server": "🖥", "db": "🗄"}


@dataclass
class Entry:
    id: int
    ts: float
    chat_id: str
    place: str
    site: str
    tool: str
    summary: str
    snapshot: str
    path: str
    backup: bytes | None
    created: int
    status: str
    undone_by: int | None

    @property
    def reversible(self) -> bool:
        if self.undone_by or self.status == "dibatalkan":
            return False
        if self.place == "server":
            return self.snapshot.startswith(("git:", "file:"))
        return self.place == "sandbox" and bool(self.path) and (self.created or self.backup is not None)

    def line(self) -> str:
        when = dt.datetime.fromtimestamp(self.ts).strftime("%d/%m %H:%M")
        where = f"{self.place}{':' + self.site if self.site else ''}"
        mark = " ↩️ dibatalkan" if self.undone_by else (" ⏪ bisa" if self.reversible else "")
        status = "" if self.status in ("ok", "dibatalkan") else f" [{self.status}]"
        return f"#{self.id} {when} {ICON.get(self.place, '•')} {where} · {self.summary[:90]}{status}{mark}"


class Journal:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.sandbox = None   # tools.files.Sandbox, diisi build()
        self.sites = None     # tools.host.Sites, diisi build()

    def add(self, chat_id: str, place: str, tool: str, summary: str, *, site: str = "",
            snapshot: str = "", path: str = "", backup: bytes | None = None, created: bool = False,
            status: str = "ok") -> int:
        if backup is not None and len(backup) > MAX_BACKUP:
            backup = None  # terlalu besar: dicatat, tapi tak bisa dibatalkan
        cur = self.db.execute(
            """INSERT INTO journal(ts, chat_id, place, site, tool, summary, snapshot, path, backup,
               created, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), chat_id, place, site, tool, summary, snapshot, path, backup, int(created), status),
        )
        self.db.commit()
        return cur.lastrowid

    def get(self, entry_id: int) -> Entry | None:
        self.db.row_factory = sqlite3.Row
        row = self.db.execute("SELECT * FROM journal WHERE id=?", (entry_id,)).fetchone()
        return Entry(**dict(row)) if row else None

    def recent(self, limit: int = 15, place: str | None = None) -> list[Entry]:
        self.db.row_factory = sqlite3.Row
        sql, args = "SELECT * FROM journal", []
        if place:
            sql += " WHERE place=?"
            args.append(place)
        rows = self.db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [Entry(**dict(r)) for r in rows]

    def render(self, limit: int = 15) -> str:
        items = self.recent(limit)
        if not items:
            return "Jurnal masih kosong."
        return "\n".join(e.line() for e in items)

    def describe_undo(self, e: Entry) -> str:
        if e.place == "server" and e.snapshot.startswith("git:"):
            return (f"Semua file terlacak git di situs {e.site} kembali ke kondisi SEBELUM #{e.id}. "
                    "Perubahan file sesudahnya juga ikut batal.")
        if e.place == "server":
            return f"File yang diubah #{e.id} di situs {e.site} dikembalikan dari salinan."
        if e.created:
            return f"File baru {e.path} di sandbox dihapus."
        return f"File {e.path} di sandbox dikembalikan ke isi sebelum #{e.id}."

    async def undo(self, e: Entry, chat_id: str) -> str:
        """Lakukan rollback (konfirmasi sudah diminta pemanggil)."""
        if not e.reversible:
            return f"#{e.id} tak bisa dibatalkan otomatis."
        if e.place == "server":
            if not self.sites:
                return "Fitur web belum aktif."
            site = self.sites.get(e.site)
            code, out, err = await self.sites.restore(site, e.snapshot)
            ok, detail = code == 0, (out + err).strip()[-500:]
        else:
            if not self.sandbox:
                return "Sandbox belum aktif."
            try:
                if e.created:
                    await self.sandbox.call("delete", path=e.path)
                else:
                    await self.sandbox.call("write", path=e.path, content=e.backup.decode("utf-8"))
                ok, detail = True, ""
            except RuntimeError as err:
                ok, detail = False, str(err)
        if not ok:
            return f"❌ Rollback #{e.id} gagal: {detail}"
        new_id = self.add(chat_id, e.place, "rollback", f"batalkan #{e.id}: {e.summary[:60]}", site=e.site)
        self.db.execute("UPDATE journal SET status='dibatalkan', undone_by=? WHERE id=?", (new_id, e.id))
        self.db.commit()
        return f"⏪ #{e.id} dibatalkan (dicatat sebagai #{new_id}).{(chr(10) + detail) if detail else ''}"
