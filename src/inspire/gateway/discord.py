"""Gateway Discord. Jawab di DM, atau di server kalau bot di-mention."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
from pathlib import Path

import discord

from .base import Gateway, command, document_text, split_text, voice_text

log = logging.getLogger(__name__)
MAX_LEN = 1900  # batas Discord 2000
MAX_DOWNLOAD = 25 * 1024 * 1024


class ConfirmView(discord.ui.View):
    def __init__(self, allowed: set[int], timeout: float, on_grant=None):
        super().__init__(timeout=timeout)
        self.allowed = allowed
        self.result = False
        self.on_grant = on_grant  # None = tak ada tombol izin 30 menit
        if on_grant:
            b = discord.ui.Button(label="Jalan & izinkan semua 30 menit", emoji="🔓",
                                  style=discord.ButtonStyle.secondary)
            b.callback = self._grant
            self.add_item(b)

    async def _grant(self, interaction: discord.Interaction) -> None:
        until = self.on_grant()
        self.result = True
        self.stop()
        await interaction.response.edit_message(
            content=f"{interaction.message.content}\n\n🔓 Disetujui + semua perintah sandbox bebas "
                    f"sampai <t:{int(until)}:t> (!kunci untuk cabut)", view=None)

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
    name = "discord"

    def __init__(self, app):
        super().__init__(app)
        cfg = app.cfg
        if not cfg.discord_token:
            raise ValueError("DISCORD_BOT_TOKEN kosong")
        self.token = cfg.discord_token
        self.allowed = set(self.owner_ids())
        self.confirm_timeout = cfg.confirm_timeout
        self.inbox_dir: Path | None = cfg.inbox_dir
        self.cmd = cfg.discord_prefix
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

    def refresh_owners(self) -> None:
        self.allowed.clear()
        self.allowed.update(self.owner_ids())  # set yang sama dipakai ConfirmView

    async def on_ready(self) -> None:
        log.info("Discord gateway jalan sebagai %s", self.client.user)

    async def _channel(self, chat_id: str):
        cid = int(chat_id.removeprefix(self.prefix))
        return self.client.get_channel(cid) or await self.client.fetch_channel(cid)

    async def send(self, chat_id: str, text: str) -> None:
        ch = await self._channel(chat_id)
        for chunk in split_text(text, MAX_LEN):
            await ch.send(chunk)

    async def send_file(self, chat_id: str, name: str, data: bytes, caption: str = "") -> None:
        ch = await self._channel(chat_id)
        await ch.send(caption[:MAX_LEN] or None, file=discord.File(io.BytesIO(data), filename=name))

    async def confirm(self, chat_id: str, text: str, grant: bool = False) -> bool:
        ch = await self._channel(chat_id)
        on_grant = (lambda: self.app.grants.grant(chat_id)) if grant else None
        view = ConfirmView(self.allowed, self.confirm_timeout, on_grant)
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
        content = message.content
        if self.client.user:
            content = content.replace(f"<@{self.client.user.id}>", "").strip()

        if message.author.id not in self.allowed:
            words = content.split()
            if is_dm and not self.allowed and words[:1] == [f"{self.cmd}pair"]:
                ok = self.try_pair(message.author.id, words[1] if len(words) > 1 else "")
                await self._reply(message, "✅ Terhubung! Kamu pemilik bot ini. Ketik "
                                  f"{self.cmd}help." if ok else "Kode pairing salah.")
                return
            log.warning("discord: user tak dikenal id=%s (%s)", message.author.id, message.author)
            return

        chat_id = f"{self.prefix}{message.channel.id}"

        if content.startswith(self.cmd):
            name, *args = content[len(self.cmd):].split()
            await self._reply(message, await command(self.app, chat_id, name, args, self.cmd))
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
                    confirm=lambda t, **kw: self.confirm(chat_id, t, **kw),
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
