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
{p}caveman auto|on|off — gaya jawaban singkat
{p}kunci — cabut izin "semua perintah 30 menit"
{p}riwayat — jurnal kerja agent; {p}rollback <nomor> — batalkan
{p}proyek — folder proyek coding; {p}tugas <deskripsi> — kerjakan di latar belakang
{p}stop — hentikan tugas; {p}diff [n] — lihat perubahan; {p}batalkan [n] — batalkan tugas terbaru
{p}setup — atur model, suara, gambar, dll (Telegram)
{p}status — fitur mana yang aktif
{p}help — bantuan ini"""


class Gateway(ABC):
    #: awalan chat_id milik gateway ini ("" = Telegram, "dc:" = Discord)
    prefix: str = ""
    #: kunci di settings.owner
    name: str = ""

    def __init__(self, app) -> None:
        self.app = app
        self.agent: Agent = app.agent
        self._bad_pair: dict[int, int] = {}

    def owns(self, chat_id: str) -> bool:
        return chat_id.startswith(self.prefix) if self.prefix else ":" not in chat_id

    def owner_ids(self) -> list[int]:
        cfg = self.app.cfg
        return cfg.allowed_user_ids if self.name == "telegram" else cfg.discord_user_ids

    def refresh_owners(self) -> None:
        """Dipanggil setelah setelan berubah (mis. pairing)."""

    def try_pair(self, user_id: int, code: str) -> bool:
        """Belum ada pemilik + kode benar → user ini jadi pemilik."""
        if self.owner_ids() or self._bad_pair.get(user_id, 0) >= 3:
            return False
        s = self.app.settings
        s.load()  # kode bisa baru dibuat proses lain (`inspire pair`)
        if code.strip() and code.strip() == s.data.get("pairing"):
            s.add_owner(self.name, user_id)
            self.app.reload()
            log.info("%s: pemilik baru %s", self.name, user_id)
            return True
        self._bad_pair[user_id] = self._bad_pair.get(user_id, 0) + 1
        return False

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, chat_id: str, text: str) -> None:
        """Kirim pesan duluan (jadwal, notif skill)."""

    async def send_code(self, chat_id: str, text: str) -> None:
        """Kirim teks sebagai blok kode (diff, log). Bawaan: teks biasa."""
        await self.send(chat_id, text)

    @abstractmethod
    async def send_file(self, chat_id: str, name: str, data: bytes, caption: str = "") -> None:
        """Kirim lampiran (hasil kerja di sandbox)."""

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


async def command(app, chat_id: str, name: str, args: list[str], p: str, confirm=None) -> str:
    """Jalankan perintah teks yang sama di semua gateway. p = awalan ('/' atau '!').
    confirm(teks) → bool, untuk perintah yang perlu tombol (rollback)."""
    agent = app.agent
    if name in ("proyek", "tugas", "stop", "diff", "batalkan"):
        if not getattr(app, "tasks", None):
            return "Mode tugas butuh sandbox (mode Docker)."
        return await app.tasks.command(chat_id, name, args, confirm, p)
    if name == "riwayat":
        n = int(args[0]) if args and args[0].isdigit() else 15
        return f"Jurnal kerja (terbaru dulu):\n\n{app.journal.render(min(n, 50))}\n\n{p}rollback <nomor> untuk membatalkan ⏪"
    if name == "rollback":
        if not args or not args[0].lstrip("#").isdigit():
            return f"Pakai: {p}rollback <nomor>. Lihat nomornya di {p}riwayat"
        e = app.journal.get(int(args[0].lstrip("#")))
        if not e:
            return "Nomor itu tak ada di jurnal."
        if not e.reversible:
            why = "sudah dibatalkan" if e.undone_by else "aksi ini tak bisa dibatalkan otomatis"
            return f"#{e.id}: {why}.\n{e.line()}"
        if not confirm or not await confirm(f"⏪ Batalkan #{e.id}?\n{e.line()}\n\n{app.journal.describe_undo(e)}"):
            return "Rollback dibatalkan."
        return await app.journal.undo(e, chat_id)
    if name == "status":
        from ..setup import render, status

        return "Status inspire:\n\n" + render(await status(app.settings, live=True))
    if name == "caveman":
        modes = {"auto": "otomatis", "on": "selalu caveman", "off": "normal"}
        if not args:
            return f"Gaya sekarang: {modes.get(app.cfg.caveman, app.cfg.caveman)}. Pilih: {p}caveman auto|on|off"
        if args[0] not in modes:
            return f"Pilih: {p}caveman auto|on|off"
        app.settings.set("style.caveman", args[0])
        app.reload()
        return {"on": "Oke. Mode caveman. Jawab pendek.", "off": "Oke, gaya normal.",
                "auto": "Oke, gaya otomatis: singkat untuk hal ringan, normal untuk penjelasan."}[args[0]]
    if name == "kunci":
        grants = getattr(app, "grants", None)
        if grants and grants.revoke(chat_id):
            return "🔒 Izin sementara dicabut. Perintah berisiko kembali minta tombol."
        return "Tak ada izin sementara yang aktif."
    if name == "setup":
        return "Setup lengkap ada di Telegram (/setup) atau di terminal server: `inspire setup`."
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
