"""Gateway Telegram (polling, tak butuh domain/HTTPS)."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..agent import Agent
from .base import Gateway, split_text

log = logging.getLogger(__name__)
MAX_LEN = 4000  # batas Telegram 4096

HELP = """Perintah:
/new — mulai sesi baru (riwayat lama tetap bisa dicari)
/model — lihat model; /model <nama> — ganti model aktif
/memory — lihat isi memori
/help — bantuan ini"""


class TelegramGateway(Gateway):
    def __init__(self, token: str, allowed_user_ids: list[int], agent: Agent):
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN kosong")
        if not allowed_user_ids:
            raise ValueError("allowed_user_ids kosong — bot menolak jalan tanpa whitelist")
        self.agent = agent
        self.app = Application.builder().token(token).build()
        only_owner = filters.User(user_id=allowed_user_ids)

        self.app.add_handler(CommandHandler(["start", "help"], self._help, filters=only_owner))
        self.app.add_handler(CommandHandler("new", self._new, filters=only_owner))
        self.app.add_handler(CommandHandler("model", self._model, filters=only_owner))
        self.app.add_handler(CommandHandler("memory", self._memory, filters=only_owner))
        self.app.add_handler(
            MessageHandler(only_owner & filters.TEXT & ~filters.COMMAND, self._text)
        )
        # Orang lain: diam, cukup catat di log.
        self.app.add_handler(MessageHandler(~only_owner, self._stranger))

    def run(self) -> None:
        log.info("Telegram gateway jalan (polling)")
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)

    async def send(self, chat_id: str, text: str) -> None:
        for chunk in split_text(text, MAX_LEN):
            await self.app.bot.send_message(chat_id=int(chat_id), text=chunk)

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

    async def _text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = str(update.effective_chat.id)
        typing = asyncio.create_task(self._keep_typing(ctx, update.effective_chat.id))
        try:
            reply = await self.agent.handle(chat_id, update.effective_message.text)
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

    async def _stranger(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        u = update.effective_user
        log.warning("pesan dari user tak dikenal: id=%s username=%s", u and u.id, u and u.username)
