"""Tool inti fase 1: memory dan search_history."""

from __future__ import annotations

import datetime as dt

from ..memory import Memory
from ..store import Store
from . import Registry, Tool, ToolContext


def register_core(reg: Registry, memory: Memory, store: Store) -> None:
    def memory_tool(
        ctx: ToolContext,
        action: str,
        target: str = "memory",
        text: str = "",
        old: str = "",
    ) -> str:
        f = memory.files.get(target)
        if not f:
            return "Error: target harus 'memory' atau 'user'."
        if action == "add":
            return f.add(text)
        if action == "replace":
            return f.replace(old, text)
        if action == "remove":
            return f.remove(old)
        return "Error: action harus add, replace, atau remove."

    reg.add(
        Tool(
            name="memory",
            description=(
                "Simpan memori jangka panjang yang selalu kamu lihat di sesi berikutnya. "
                "target='user' untuk fakta tentang user (nama, preferensi, kebiasaan); "
                "target='memory' untuk catatanmu sendiri (pelajaran, info lingkungan, "
                "keputusan). Entri harus pendek dan padat. Kalau penuh, ringkas/hapus "
                "entri lama. Jangan simpan hal sepele atau yang cuma berlaku hari ini."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                    "target": {"type": "string", "enum": ["memory", "user"]},
                    "text": {"type": "string", "description": "Isi baru (add/replace)."},
                    "old": {
                        "type": "string",
                        "description": "Potongan teks unik dari entri lama (replace/remove).",
                    },
                },
                "required": ["action", "target"],
            },
            fn=memory_tool,
        )
    )

    def search_history(ctx: ToolContext, query: str, limit: int = 8) -> str:
        hits = store.search(query, None, limit=min(limit, 20))
        if not hits:
            return "Tidak ketemu."
        lines = []
        for h in hits:
            when = dt.datetime.fromtimestamp(h["created_at"]).strftime("%Y-%m-%d %H:%M")
            where = "discord" if h["chat_id"].startswith("dc:") else "telegram"
            lines.append(f"[{when} {where} sesi#{h['session_id']} {h['role']}] {h['snip']}")
        return "\n".join(lines)

    reg.add(
        Tool(
            name="search_history",
            description=(
                "Cari di seluruh riwayat chat lama (semua sesi, Telegram & Discord) dengan kata kunci. "
                "Pakai kalau user menyebut hal dari obrolan dulu yang tidak ada di "
                "memori atau konteks sekarang."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Kata kunci, spasi = OR."},
                    "limit": {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
            fn=search_history,
        )
    )


def register_journal(reg: Registry, journal) -> None:
    def riwayat_kerja(ctx: ToolContext, limit: int = 15, place: str = "") -> str:
        items = journal.recent(min(limit, 50), place or None)
        return "\n".join(e.line() for e in items) or "Jurnal masih kosong."

    reg.add(
        Tool(
            name="riwayat_kerja",
            description=(
                "Lihat jurnal aksimu yang mengubah sesuatu (file/perintah di sandbox & server, query DB), "
                "terbaru dulu. Pakai untuk menjawab 'tadi/kemarin kamu ubah apa?'. User membatalkan "
                "dengan /rollback <nomor>; sebutkan nomornya kalau relevan."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 15},
                    "place": {"type": "string", "enum": ["", "sandbox", "server", "db"]},
                },
            },
            fn=riwayat_kerja,
        )
    )
