"""Gateway Telegram (polling, tak butuh domain/HTTPS)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..agent import Agent
from ..media import MediaError
from ..tools import ToolContext
from .base import Gateway, split_text

log = logging.getLogger(__name__)
MAX_LEN = 4000  # batas Telegram 4096
MAX_DOWNLOAD = 20 * 1024 * 1024  # batas unduh Bot API

HELP = """Perintah:
/new — mulai sesi baru (riwayat lama tetap bisa dicari)
/model — lihat model; /model <nama> — ganti model aktif
/memory — lihat isi memori
/jobs — lihat jadwal aktif
/help — bantuan ini"""


class TelegramGateway(Gateway):
    def __init__(
        self,
        token: str,
        allowed_user_ids: list[int],
        agent: Agent,
        confirm_timeout: int = 300,
        inbox_dir: Path | None = None,
    ):
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN kosong")
        if not allowed_user_ids:
            raise ValueError("allowed_user_ids kosong — bot menolak jalan tanpa whitelist")
        self.agent = agent
        self.allowed = set(allowed_user_ids)
        self.confirm_timeout = confirm_timeout
        self.inbox_dir = inbox_dir
        self._pending: dict[str, asyncio.Future] = {}
        self._on_start: list[Callable[[], None]] = []
        self.app = (
            Application.builder()
            .token(token)
            # Wajib: handler pesan menunggu tombol ✅/❌, jadi update tombol
            # harus bisa diproses bersamaan. Urutan per chat dijaga lock di Agent.
            .concurrent_updates(True)
            .post_init(self._post_init)
            .build()
        )
        only_owner = filters.User(user_id=allowed_user_ids)

        self.app.add_handler(CommandHandler(["start", "help"], self._help, filters=only_owner))
        self.app.add_handler(CommandHandler("new", self._new, filters=only_owner))
        self.app.add_handler(CommandHandler("model", self._model, filters=only_owner))
        self.app.add_handler(CommandHandler("memory", self._memory, filters=only_owner))
        self.app.add_handler(CommandHandler("jobs", self._jobs, filters=only_owner))
        self.app.add_handler(CallbackQueryHandler(self._button, pattern=r"^(ok|no):"))
        self.app.add_handler(
            MessageHandler(only_owner & filters.TEXT & ~filters.COMMAND, self._text)
        )
        self.app.add_handler(
            MessageHandler(only_owner & (filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE), self._voice)
        )
        self.app.add_handler(
            MessageHandler(only_owner & (filters.PHOTO | filters.Document.IMAGE), self._photo)
        )
        self.app.add_handler(MessageHandler(only_owner & filters.Document.ALL, self._document))
        self.app.add_handler(MessageHandler(only_owner & filters.VIDEO, self._video))
        # Orang lain: diam, cukup catat di log.
        self.app.add_handler(MessageHandler(~only_owner, self._stranger))

    def on_start(self, fn: Callable[[], None]) -> None:
        """Daftarkan fungsi yang jalan sekali di dalam event loop bot (mis. scheduler)."""
        self._on_start.append(fn)

    async def _post_init(self, _: Application) -> None:
        for fn in self._on_start:
            fn()

    def run(self) -> None:
        log.info("Telegram gateway jalan (polling)")
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)

    async def send(self, chat_id: str, text: str) -> None:
        for chunk in split_text(text, MAX_LEN):
            await self.app.bot.send_message(chat_id=int(chat_id), text=chunk)

    async def confirm(self, chat_id: str, text: str) -> bool:
        """Kirim tombol ✅/❌, tunggu jawaban. Diam sampai timeout = tolak."""
        key = uuid.uuid4().hex[:16]
        fut = asyncio.get_running_loop().create_future()
        self._pending[key] = fut
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Jalan", callback_data=f"ok:{key}"),
                InlineKeyboardButton("❌ Batal", callback_data=f"no:{key}"),
            ]]
        )
        msg = await self.app.bot.send_message(int(chat_id), text[:4000], reply_markup=kb)
        try:
            ok = await asyncio.wait_for(fut, self.confirm_timeout)
        except asyncio.TimeoutError:
            ok = False
            with contextlib.suppress(Exception):
                await msg.edit_text(f"{msg.text}\n\n⌛ Tak dijawab — dibatalkan.")
        finally:
            self._pending.pop(key, None)
        return ok

    def confirm_for(self, chat_id: str):
        return lambda text: self.confirm(chat_id, text)

    async def _button(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not q.from_user or q.from_user.id not in self.allowed:
            await q.answer("Bukan untukmu.")
            return
        action, key = q.data.split(":", 1)
        fut = self._pending.get(key)
        if not fut or fut.done():
            await q.answer("Sudah kedaluwarsa.")
            with contextlib.suppress(Exception):
                await q.edit_message_reply_markup(None)
            return
        fut.set_result(action == "ok")
        await q.answer()
        label = "✅ Disetujui" if action == "ok" else "❌ Dibatalkan"
        with contextlib.suppress(Exception):
            await q.edit_message_text(f"{q.message.text}\n\n{label}")

    # --- handler --------------------------------------------------------

    async def _reply(self, update: Update, text: str) -> None:
        for chunk in split_text(text, MAX_LEN):
            await update.effective_message.reply_text(chunk)

    async def _help(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, f"Halo, aku inspire.\n\n{HELP}")

    async def _new(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        s = self.agent.new_session(str(update.effective_chat.id))
        await self._reply(update, f"Sesi baru #{s.id} dimulai.")

    async def _model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        llm = self.agent.llm
        if ctx.args:
            name = ctx.args[0]
            if llm.set_active(name):
                await self._reply(update, f"Model aktif: {name} ({llm.models[name].model})")
            else:
                await self._reply(update, f"Tak ada model '{name}'.")
            return
        lines = []
        for name, cfg in llm.models.items():
            mark = "▶" if name == llm.active else " "
            fb = " (fallback)" if name in llm.fallback else ""
            lines.append(f"{mark} {name} — {cfg.model}{fb}")
        await self._reply(update, "Model:\n" + "\n".join(lines) + "\n\n/model <nama> untuk ganti")

    async def _memory(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, self.agent.memory.snapshot())

    async def _jobs(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        tool = self.agent.tools.tools.get("schedule")
        if not tool:
            await self._reply(update, "Jadwal belum aktif.")
            return
        out = await self.agent.tools.call(
            "schedule", {"action": "list"}, ToolContext(str(update.effective_chat.id), 0)
        )
        await self._reply(update, out)

    async def _run_agent(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        text: str,
        images: list[tuple[bytes, str]] | None = None,
    ) -> None:
        chat_id = str(update.effective_chat.id)
        typing = asyncio.create_task(self._keep_typing(ctx, update.effective_chat.id))
        try:
            reply = await self.agent.handle(
                chat_id, text, confirm=self.confirm_for(chat_id), images=images
            )
        except Exception as e:
            log.exception("handle gagal")
            reply = f"⚠️ Error: {type(e).__name__}: {e}"
        finally:
            typing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing
        await self._reply(update, reply)

    async def _text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_agent(update, ctx, update.effective_message.text)

    async def _download(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, file_obj) -> bytes | None:
        if (file_obj.file_size or 0) > MAX_DOWNLOAD:
            await self._reply(update, "⚠️ File > 20MB, Telegram tak izinkan bot mengunduhnya.")
            return None
        f = await ctx.bot.get_file(file_obj.file_id)
        return bytes(await f.download_as_bytearray())

    async def _voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        m = update.effective_message
        obj = m.voice or m.audio or m.video_note
        media = self.agent.media
        if not media:
            await self._reply(update, "⚠️ Fitur suara belum diatur.")
            return
        data = await self._download(update, ctx, obj)
        if data is None:
            return
        name = getattr(obj, "file_name", None) or ("voice.ogg" if m.voice else "audio.mp4")
        await ctx.bot.send_chat_action(m.chat_id, ChatAction.TYPING)
        try:
            transcript = await media.transcribe(data, name)
        except MediaError as e:
            await self._reply(update, f"⚠️ Gagal transkrip: {e}")
            return
        if not transcript:
            await self._reply(update, "🎙 (tak ada suara yang terdengar)")
            return
        # Tampilkan transkrip supaya user tahu kalau salah dengar.
        await self._reply(update, f"🎙 {transcript[:500]}{'…' if len(transcript) > 500 else ''}")
        text = f"[🎙 Voice note, transkrip otomatis] {transcript}"
        if m.caption:
            text += f"\nKeterangan: {m.caption}"
        await self._run_agent(update, ctx, text)

    async def _photo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        m = update.effective_message
        if m.photo:
            obj, mime = m.photo[-1], "image/jpeg"  # ukuran terbesar
        else:
            obj, mime = m.document, m.document.mime_type or "image/png"
        data = await self._download(update, ctx, obj)
        if data is None:
            return
        await self._run_agent(update, ctx, m.caption or "", images=[(data, mime)])

    async def _document(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        m = update.effective_message
        doc = m.document
        data = await self._download(update, ctx, doc)
        if data is None:
            return
        name = _safe_name(doc.file_name or "file")
        saved = self._save_inbox(name, data)
        where = f" — disalin ke sandbox: /workspace/inbox/{saved}" if saved else ""
        try:
            body = self.agent.media.extract_file(data, name) if self.agent.media else ""
            text = f"[📎 File dari user: {name}{where}]\n{body}"
        except MediaError as e:
            text = f"[📎 File dari user: {name}{where}]\n(Isi tak bisa dibaca langsung: {e})"
        if m.caption:
            text += f"\n\nPesan user: {m.caption}"
        await self._run_agent(update, ctx, text)

    async def _video(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, "🎬 File video belum didukung. Kirim link-nya saja (YouTube, dll).")

    def _save_inbox(self, name: str, data: bytes) -> str | None:
        """Simpan salinan file ke inbox (folder yang juga terlihat di sandbox)."""
        if not self.inbox_dir:
            return None
        try:
            self.inbox_dir.mkdir(parents=True, exist_ok=True)
            final = f"{time.strftime('%Y%m%d-%H%M%S')}-{name}"
            (self.inbox_dir / final).write_bytes(data)
            return final
        except OSError:
            log.exception("gagal simpan ke inbox")
            return None

    async def _keep_typing(self, ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
        while True:
            with contextlib.suppress(Exception):
                await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4)

    async def _stranger(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        u = update.effective_user
        log.warning("pesan dari user tak dikenal: id=%s username=%s", u and u.id, u and u.username)


def _safe_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^\w.\-]+", "_", name)[:120] or "file"
