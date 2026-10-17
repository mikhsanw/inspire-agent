"""Gateway Discord. Jawab di DM, atau di server kalau bot di-mention."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import discord

from ..agent import Agent
from .base import Gateway, command, document_text, split_text, voice_text

log = logging.getLogger(__name__)
MAX_LEN = 1900  # batas Discord 2000
MAX_DOWNLOAD = 25 * 1024 * 1024


class ConfirmView(discord.ui.View):
    def __init__(self, allowed: set[int], timeout: float):
        super().__init__(timeout=timeout)
        self.allowed = allowed
        self.result = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in self.allowed:
            await interaction.response.send_message("Bukan untukmu.", ephemeral=True)
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, ok: bool) -> None:
        self.result = ok
        self.stop()
        label = "✅ Disetujui" if ok else "❌ Dibatalkan"
        await interaction.response.edit_message(
            content=f"{interaction.message.content}\n\n{label}", view=None
        )

    @discord.ui.button(label="Jalan", emoji="✅", style=discord.ButtonStyle.success)
    async def ok(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._finish(interaction, True)

    @discord.ui.button(label="Batal", emoji="❌", style=discord.ButtonStyle.danger)
    async def no(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._finish(interaction, False)


class DiscordGateway(Gateway):
    prefix = "dc:"

    def __init__(
        self,
        token: str,
        allowed_user_ids: list[int],
        agent: Agent,
        confirm_timeout: int = 300,
        inbox_dir: Path | None = None,
        command_prefix: str = "!",
    ):
        if not token:
            raise ValueError("DISCORD_BOT_TOKEN kosong")
        if not allowed_user_ids:
            raise ValueError("discord allowed_user_ids kosong — bot menolak jalan tanpa whitelist")
        self.token = token
        self.agent = agent
        self.allowed = set(allowed_user_ids)
        self.confirm_timeout = confirm_timeout
        self.inbox_dir = inbox_dir
        self.cmd = command_prefix
        intents = discord.Intents.default()
        intents.message_content = True  # aktifkan juga "Message Content Intent" di Developer Portal
        self.client = discord.Client(intents=intents)
        self.client.event(self.on_ready)
        self.client.event(self.on_message)
        self._task: asyncio.Task | None = None

    # --- siklus hidup ---------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self.client.start(self.token))
        ready = asyncio.create_task(self.client.wait_until_ready())
        done, _ = await asyncio.wait({self._task, ready}, return_when=asyncio.FIRST_COMPLETED)
        if self._task in done:  # login gagal (token salah, dll)
            ready.cancel()
            self._task.result()

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            await self.client.close()

    async def on_ready(self) -> None:
        log.info("Discord gateway jalan sebagai %s", self.client.user)

    async def _channel(self, chat_id: str):
        cid = int(chat_id.removeprefix(self.prefix))
        return self.client.get_channel(cid) or await self.client.fetch_channel(cid)

    async def send(self, chat_id: str, text: str) -> None:
        ch = await self._channel(chat_id)
        for chunk in split_text(text, MAX_LEN):
            await ch.send(chunk)

    async def confirm(self, chat_id: str, text: str) -> bool:
        ch = await self._channel(chat_id)
        view = ConfirmView(self.allowed, self.confirm_timeout)
        msg = await ch.send(text[:MAX_LEN], view=view)
        if await view.wait():  # True = timeout
            with contextlib.suppress(Exception):
                await msg.edit(content=f"{msg.content}\n\n⌛ Tak dijawab — dibatalkan.", view=None)
            return False
        return view.result

    # --- pesan masuk ----------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        is_dm = message.guild is None
        if not is_dm and self.client.user not in message.mentions:
            return  # di server: hanya kalau di-mention
        if message.author.id not in self.allowed:
            log.warning("discord: user tak dikenal id=%s (%s)", message.author.id, message.author)
            return

        chat_id = f"{self.prefix}{message.channel.id}"
        content = message.content
        if self.client.user:
            content = content.replace(f"<@{self.client.user.id}>", "").strip()

        if content.startswith(self.cmd):
            name, *args = content[len(self.cmd):].split()
            await self._reply(message, await command(self.agent, chat_id, name, args, self.cmd))
            return

        images: list[tuple[bytes, str]] = []
        parts: list[str] = []
        for a in message.attachments:
            if a.size > MAX_DOWNLOAD:
                await self._reply(message, f"⚠️ {a.filename} > 25MB, dilewati.")
                continue
            data = await a.read()
            ctype = a.content_type or ""
            if a.is_voice_message() or ctype.startswith("audio/"):
                text, shown = await voice_text(self.agent, data, a.filename or "voice.ogg")
                await self._reply(message, shown)
                if text:
                    parts.append(text)
            elif ctype.startswith("image/"):
                images.append((data, ctype))
            elif ctype.startswith("video/"):
                await self._reply(message, "🎬 File video belum didukung. Kirim link-nya saja.")
            else:
                parts.append(document_text(self.agent, self.inbox_dir, a.filename, data, ""))
        if content:
            parts.append(content if not parts else f"Pesan user: {content}")
        if not parts and not images:
            return

        try:
            async with message.channel.typing():
                reply = await self.agent.handle(
                    chat_id,
                    "\n\n".join(parts),
                    confirm=lambda t: self.confirm(chat_id, t),
                    images=images or None,
                )
        except Exception as e:
            log.exception("handle gagal")
            reply = f"⚠️ Error: {type(e).__name__}: {e}"
        await self._reply(message, reply)

    async def _reply(self, message: discord.Message, text: str) -> None:
        for i, chunk in enumerate(split_text(text, MAX_LEN)):
            if i == 0:
                await message.reply(chunk, mention_author=False)
            else:
                await message.channel.send(chunk)
