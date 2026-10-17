"""Gateway Telegram (polling, tak butuh domain/HTTPS)."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import time
import uuid
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .base import Gateway, command, document_text, split_text, voice_text
from .tg_setup import TelegramSetup

log = logging.getLogger(__name__)


def _code_chunks(text: str, limit: int = 3500) -> list[str]:
    """Pecah per baris sampai panjang SETELAH di-escape HTML masih muat di satu pesan."""
    chunks, cur, size = [], [], 0
    for line in text.splitlines():
        line = line[:1500]
        n = len(html.escape(line)) + 1
        if cur and size + n > limit:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += n
    if cur:
        chunks.append("\n".join(cur))
    return chunks or [""]


def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


MAX_LEN = 4000  # batas Telegram 4096
MAX_DOWNLOAD = 20 * 1024 * 1024  # batas unduh Bot API
COMMANDS = ["start", "help", "new", "model", "memory", "jobs", "skills", "skillundo", "status",
            "caveman", "kunci", "riwayat", "rollback", "proyek", "tugas", "stop", "diff", "batalkan"]


class TelegramGateway(Gateway):
    prefix = ""
    name = "telegram"

    def __init__(self, app):
        super().__init__(app)
        cfg = app.cfg
        if not cfg.telegram_token:
            raise ValueError("TELEGRAM_BOT_TOKEN kosong")
        self.confirm_timeout = cfg.confirm_timeout
        self.inbox_dir: Path | None = cfg.inbox_dir
        self._pending: dict[str, asyncio.Future] = {}
        self.setup = TelegramSetup(app)
        self.tg = (
            Application.builder()
            .token(cfg.telegram_token)
            # Wajib: handler pesan menunggu tombol ✅/❌, jadi update tombol
            # harus bisa diproses bersamaan. Urutan per chat dijaga lock di Agent.
            .concurrent_updates(True)
            .build()
        )
        # Filter pemilik bisa diubah saat jalan (pairing) → refresh_owners().
        self.owner = filters.User(user_id=self.owner_ids())
        owner = self.owner
        add = self.tg.add_handler
        add(CommandHandler(COMMANDS, self._command, filters=owner))
        add(CommandHandler("setup", self._setup, filters=owner))
        add(CallbackQueryHandler(self._button, pattern=r"^(ok|no|gr):"))
        add(CallbackQueryHandler(self._setup_button, pattern=r"^su:"))
        add(MessageHandler(owner & filters.TEXT & ~filters.COMMAND, self._text))
        add(MessageHandler(owner & (filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE), self._voice))
        add(MessageHandler(owner & (filters.PHOTO | filters.Document.IMAGE), self._photo))
        add(MessageHandler(owner & filters.Document.ALL, self._document))
        add(MessageHandler(owner & filters.VIDEO, self._video))
        add(MessageHandler(~owner, self._stranger))  # pairing, atau diam

    # --- siklus hidup ---------------------------------------------------

    def refresh_owners(self) -> None:
        self.owner.user_ids = self.owner_ids()

    async def start(self) -> None:
        await self.tg.initialize()
        await self.tg.start()
        await self.tg.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        log.info("Telegram gateway jalan (polling) sebagai @%s", self.tg.bot.username)

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            await self.tg.updater.stop()
            await self.tg.stop()
            await self.tg.shutdown()

    async def send(self, chat_id: str, text: str) -> None:
        for chunk in split_text(text, MAX_LEN):
            await self.tg.bot.send_message(chat_id=int(chat_id), text=chunk)

    async def send_code(self, chat_id: str, text: str) -> None:
        for chunk in _code_chunks(text):
            await self.tg.bot.send_message(int(chat_id), f"<pre>{html.escape(chunk)}</pre>", parse_mode="HTML")

    async def send_file(self, chat_id: str, name: str, data: bytes, caption: str = "") -> None:
        await self.tg.bot.send_document(int(chat_id), InputFile(data, filename=name), caption=caption[:1000] or None)

    async def confirm(self, chat_id: str, text: str, grant: bool = False) -> bool:
        key = uuid.uuid4().hex[:16]
        fut = asyncio.get_running_loop().create_future()
        self._pending[key] = (fut, chat_id)
        rows = [[
            InlineKeyboardButton("✅ Jalan", callback_data=f"ok:{key}"),
            InlineKeyboardButton("❌ Batal", callback_data=f"no:{key}"),
        ]]
        if grant:
            rows.append([InlineKeyboardButton("✅ Jalan & izinkan semua 30 menit", callback_data=f"gr:{key}")])
        kb = InlineKeyboardMarkup(rows)
        msg = await self.tg.bot.send_message(int(chat_id), text[:4000], reply_markup=kb)
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
        if not q.from_user or q.from_user.id not in self.owner_ids():
            await q.answer("Bukan untukmu.")
            return
        action, key = q.data.split(":", 1)
        fut, chat_id = self._pending.get(key, (None, ""))
        if not fut or fut.done():
            await q.answer("Sudah kedaluwarsa.")
            with contextlib.suppress(Exception):
                await q.edit_message_reply_markup(None)
            return
        label = {"ok": "✅ Disetujui", "no": "❌ Dibatalkan"}.get(action, "")
        if action == "gr":
            until = self.app.grants.grant(chat_id)
            label = f"🔓 Disetujui + semua perintah sandbox bebas sampai {_hhmm(until)} (/kunci untuk cabut)"
        fut.set_result(action in ("ok", "gr"))
        await q.answer()
        with contextlib.suppress(Exception):
            await q.edit_message_text(f"{q.message.text}\n\n{label}")

    # --- handler --------------------------------------------------------

    async def _reply(self, update: Update, text: str) -> None:
        for chunk in split_text(text, MAX_LEN):
            await update.effective_message.reply_text(chunk)

    async def _command(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        name = update.effective_message.text.split()[0].lstrip("/").split("@")[0]
        chat_id = str(update.effective_chat.id)
        out = await command(self.app, chat_id, name, ctx.args or [], "/",
                            confirm=lambda t: self.confirm(chat_id, t))
        if out:  # kosong = perintah sudah mengirim sendiri (mis. /diff)
            await self._reply(update, out)

    async def _setup(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await self.setup.menu(update)

    async def _setup_button(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.callback_query.from_user or update.callback_query.from_user.id not in self.owner_ids():
            await update.callback_query.answer("Bukan untukmu.")
            return
        await self.setup.on_button(update)

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
                chat_id, text, confirm=lambda t, **kw: self.confirm(chat_id, t, **kw), images=images
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
        if self.setup.is_waiting(str(update.effective_chat.id)):
            await self.setup.on_text(update)  # balasan untuk /setup (mis. key)
            return
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
        if data is not None and self.setup.is_waiting(str(update.effective_chat.id)):
            await self.setup.on_document(update, data)  # mis. cookies.txt
            return
        if data is not None:
            text = document_text(
                self.agent, self.inbox_dir, m.document.file_name or "file", data, m.caption or ""
            )
            await self._run_agent(update, ctx, text)

    async def _video(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, "🎬 File video belum didukung. Kirim link-nya saja (YouTube, dll).")

    async def _stranger(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        u, m = update.effective_user, update.effective_message
        if not u or not m:
            return
        text = (m.text or "").strip()
        # /pair 123456, atau /start 123456 (dari tautan t.me/bot?start=123456)
        if not self.owner_ids() and text.split()[:1] in (["/pair"], ["/start"]):
            code = text.split()[1] if len(text.split()) > 1 else ""
            if self.try_pair(u.id, code):
                await m.reply_text(
                    "✅ Terhubung! Kamu pemilik bot ini.\n\n"
                    "Ketik /setup untuk atur model & fitur, /status untuk cek, atau langsung ngobrol."
                )
            else:
                await m.reply_text("Kode pairing salah. Lihat kodenya di server: `inspire pair`.")
            return
        log.warning("pesan dari user tak dikenal: id=%s username=%s", u.id, u.username)
