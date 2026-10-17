"""Interface gateway + logika bersama. Agent tidak tahu ia di Telegram atau Discord."""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path

from ..agent import Agent
from ..media import MediaError

log = logging.getLogger(__name__)

HELP = """Perintah:
{p}new — mulai sesi baru (riwayat lama tetap bisa dicari)
{p}model — lihat model; {p}model <nama> — ganti model aktif
{p}memory — lihat isi memori
{p}jobs — lihat jadwal aktif
{p}skills — daftar skill + riwayat perubahan
{p}skillundo — batalkan perubahan skill terakhir
{p}help — bantuan ini"""


class Gateway(ABC):
    #: awalan chat_id milik gateway ini ("" = Telegram, "dc:" = Discord)
    prefix: str = ""

    def owns(self, chat_id: str) -> bool:
        return chat_id.startswith(self.prefix) if self.prefix else ":" not in chat_id

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, chat_id: str, text: str) -> None:
        """Kirim pesan duluan (jadwal, notif skill)."""

    @abstractmethod
    async def confirm(self, chat_id: str, text: str) -> bool:
        """Tombol ✅/❌. Tak dijawab sampai timeout = False."""


def split_text(text: str, size: int) -> list[str]:
    """Pecah teks panjang, utamakan potong di baris kosong / baris baru."""
    chunks = []
    while len(text) > size:
        cut = text.rfind("\n\n", 0, size)
        if cut < size // 2:
            cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def safe_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^\w.\-]+", "_", name)[:120] or "file"


# --- perintah bersama ------------------------------------------------------


async def command(agent: Agent, chat_id: str, name: str, args: list[str], p: str) -> str:
    """Jalankan perintah teks yang sama di semua gateway. p = awalan ('/' atau '!')."""
    if name in ("start", "help"):
        return f"Halo, aku inspire.\n\n{HELP.format(p=p)}"
    if name == "new":
        return f"Sesi baru #{agent.new_session(chat_id).id} dimulai."
    if name == "model":
        llm = agent.llm
        if args:
            if llm.set_active(args[0]):
                return f"Model aktif: {args[0]} ({llm.models[args[0]].model})"
            return f"Tak ada model '{args[0]}'."
        lines = [
            f"{'▶' if n == llm.active else ' '} {n} — {c.model}"
            f"{' (fallback)' if n in llm.fallback else ''}{' 👁' if c.vision else ''}"
            for n, c in llm.models.items()
        ]
        return "Model:\n" + "\n".join(lines) + f"\n\n{p}model <nama> untuk ganti"
    if name == "memory":
        return agent.memory.snapshot()
    if name == "jobs":
        if "schedule" not in agent.tools.tools:
            return "Jadwal belum aktif."
        from ..tools import ToolContext

        return await agent.tools.call("schedule", {"action": "list"}, ToolContext(chat_id, 0))
    if name == "skills":
        if not agent.skills:
            return "Skill belum aktif."
        return f"Skill:\n{agent.skills.index()}\n\nRiwayat:\n{agent.skills.history()}"
    if name == "skillundo":
        return agent.skills.undo() if agent.skills else "Skill belum aktif."
    return f"Perintah tak dikenal. {p}help"


# --- media bersama ---------------------------------------------------------


async def voice_text(agent: Agent, data: bytes, filename: str) -> tuple[str | None, str]:
    """Kembalikan (teks untuk agent atau None, pesan untuk user)."""
    if not agent.media:
        return None, "⚠️ Fitur suara belum diatur."
    try:
        transcript = await agent.media.transcribe(data, filename)
    except MediaError as e:
        return None, f"⚠️ Gagal transkrip: {e}"
    if not transcript:
        return None, "🎙 (tak ada suara yang terdengar)"
    shown = f"🎙 {transcript[:500]}{'…' if len(transcript) > 500 else ''}"
    return f"[🎙 Voice note, transkrip otomatis] {transcript}", shown


def document_text(agent: Agent, inbox: Path | None, filename: str, data: bytes, caption: str) -> str:
    name = safe_name(filename)
    saved = save_inbox(inbox, name, data)
    where = f" — disalin ke sandbox: /workspace/inbox/{saved}" if saved else ""
    try:
        body = agent.media.extract_file(data, name) if agent.media else ""
        text = f"[📎 File dari user: {name}{where}]\n{body}"
    except MediaError as e:
        text = f"[📎 File dari user: {name}{where}]\n(Isi tak bisa dibaca langsung: {e})"
    if caption:
        text += f"\n\nPesan user: {caption}"
    return text


def save_inbox(inbox: Path | None, name: str, data: bytes) -> str | None:
    """Simpan salinan file ke inbox (folder yang juga terlihat di sandbox)."""
    if not inbox:
        return None
    try:
        inbox.mkdir(parents=True, exist_ok=True)
        final = f"{time.strftime('%Y%m%d-%H%M%S')}-{name}"
        (inbox / final).write_bytes(data)
        return final
    except OSError:
        log.exception("gagal simpan ke inbox")
        return None
