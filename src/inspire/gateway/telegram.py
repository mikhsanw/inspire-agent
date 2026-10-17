"""Gateway Telegram (polling, tak butuh domain/HTTPS)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from pathlib import Path

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
from .base import Gateway, command, document_text, split_text, voice_text

log = logging.getLogger(__name__)
MAX_LEN = 4000  # batas Telegram 4096
MAX_DOWNLOAD = 20 * 1024 * 1024  # batas unduh Bot API
COMMANDS = ["start", "help", "new", "model", "memory", "jobs", "skills", "skillundo"]


class TelegramGateway(Gateway):
    prefix = ""

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
            raise ValueError("telegram allowed_user_ids kosong — bot menolak jalan tanpa whitelist")
        self.agent = agent
        self.allowed = set(allowed_user_ids)
        self.confirm_timeout = confirm_timeout
        self.inbox_dir = inbox_dir
        self._pending: dict[str, asyncio.Future] = {}
        self.app = (
            Application.builder()
            .token(token)
            # Wajib: handler pesan menunggu tombol ✅/❌, jadi update tombol
            # harus bisa diproses bersamaan. Urutan per chat dijaga lock di Agent.
            .concurrent_updates(True)
            .build()
        )
        owner = filters.User(user_id=allowed_user_ids)
        add = self.app.add_handler
        add(CommandHandler(COMMANDS, self._command, filters=owner))
        add(CallbackQueryHandler(self._button, pattern=r"^(ok|no):"))
        add(MessageHandler(owner & filters.TEXT & ~filters.COMMAND, self._text))
        add(MessageHandler(owner & (filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE), self._voice))
        add(MessageHandler(owner & (filters.PHOTO | filters.Document.IMAGE), self._photo))
        add(MessageHandler(owner & filters.Document.ALL, self._document))
        add(MessageHandler(owner & filters.VIDEO, self._video))
        add(MessageHandler(~owner, self._stranger))  # orang lain: diam, catat di log

    # --- siklus hidup ---------------------------------------------------

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        log.info("Telegram gateway jalan (polling)")

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()

    async def send(self, chat_id: str, text: str) -> None:
        for chunk in split_text(text, MAX_LEN):
            await self.app.bot.send_message(chat_id=int(chat_id), text=chunk)

    async def confirm(self, chat_id: str, text: str) -> bool:
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
            return await asyncio.wait_for(fut, self.confirm_timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                await msg.edit_text(f"{msg.text}\n\n⌛ Tak dijawab — dibatalkan.")
            return False
        finally:
            self._pending.pop(key, None)

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

    async def _command(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        name = update.effective_message.text.split()[0].lstrip("/").split("@")[0]
        out = await command(self.agent, str(update.effective_chat.id), name, ctx.args or [], "/")
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
                chat_id, text, confirm=lambda t: self.confirm(chat_id, t), images=images
            )
        except Exception as e:
            log.exception("handle gagal")
            reply = f"⚠️ Error: {type(e).__name__}: {e}"
        finally:
            typing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing
        await self._reply(update, reply)

    async def _keep_typing(self, ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
        while True:
            with contextlib.suppress(Exception):
                await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4)

    async def _download(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, obj) -> bytes | None:
        if (obj.file_size or 0) > MAX_DOWNLOAD:
            await self._reply(update, "⚠️ File > 20MB, Telegram tak izinkan bot mengunduhnya.")
            return None
        f = await ctx.bot.get_file(obj.file_id)
        return bytes(await f.download_as_bytearray())

    async def _text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_agent(update, ctx, update.effective_message.text)

    async def _voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        m = update.effective_message
        obj = m.voice or m.audio or m.video_note
        data = await self._download(update, ctx, obj)
        if data is None:
            return
        name = getattr(obj, "file_name", None) or ("voice.ogg" if m.voice else "audio.mp4")
        await ctx.bot.send_chat_action(m.chat_id, ChatAction.TYPING)
        text, shown = await voice_text(self.agent, data, name)
        await self._reply(update, shown)  # tampilkan transkrip / error
        if text:
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
        if data is not None:
            await self._run_agent(update, ctx, m.caption or "", images=[(data, mime)])

    async def _document(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        m = update.effective_message
        data = await self._download(update, ctx, m.document)
        if data is not None:
            text = document_text(
                self.agent, self.inbox_dir, m.document.file_name or "file", data, m.caption or ""
            )
            await self._run_agent(update, ctx, text)

    async def _video(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, "🎬 File video belum didukung. Kirim link-nya saja (YouTube, dll).")

    async def _stranger(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        u = update.effective_user
        log.warning("pesan dari user tak dikenal: id=%s username=%s", u and u.id, u and u.username)
