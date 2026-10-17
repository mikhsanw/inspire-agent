"""/setup di Telegram: menu tombol per fitur, tempel key, lewati, hapus.

Pesan berisi key langsung dihapus setelah dibaca.
"""

from __future__ import annotations

import contextlib
import logging
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup, Update

from .. import setup as S
from ..setup import checks

log = logging.getLogger(__name__)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn(t, callback_data=d) for t, d in row] for row in rows if row])


BACK = ("⬅ Menu", "su:menu")


class TelegramSetup:
    def __init__(self, app):
        self.app = app
        # chat_id → apa yang sedang ditunggu: {"kind": ..., ...}
        self.waiting: dict[str, dict] = {}

    @property
    def settings(self):
        return self.app.settings

    def is_waiting(self, chat_id: str) -> bool:
        return chat_id in self.waiting

    # --- menu -----------------------------------------------------------

    async def menu(self, update: Update) -> None:
        self.waiting.pop(str(update.effective_chat.id), None)
        feats = {f.id: f for f in await S.status(self.settings)}
        buttons = [(f"{feats[sid].icon} {S.TITLES[sid]}", f"su:sec:{sid}") for sid in S.ORDER]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        text = (
            "⚙️ Setup inspire\n\n✅ aktif  ⏸ dilewati  ⬜ belum  ❌ perlu dibereskan\n\n"
            "Pilih bagian. Semua bisa dilewati dan diisi nanti."
        )
        await self._show(update, text, kb(*rows, [("📋 Status lengkap", "su:status")]))

    async def _show(self, update: Update, text: str, markup=None) -> None:
        q = update.callback_query
        if q:
            with contextlib.suppress(Exception):
                await q.edit_message_text(text, reply_markup=markup, disable_web_page_preview=True)
                return
        await update.effective_chat.send_message(text, reply_markup=markup, disable_web_page_preview=True)

    # --- tombol ---------------------------------------------------------

    async def on_button(self, update: Update) -> None:
        q = update.callback_query
        await q.answer()
        chat_id = str(update.effective_chat.id)
        parts = q.data.split(":")
        action, arg = parts[1], (parts[2] if len(parts) > 2 else "")

        if action == "menu":
            return await self.menu(update)
        if action == "status":
            text = S.render(await S.status(self.settings, live=True))
            return await self._show(update, f"📋 Status\n\n{text}", kb([BACK]))
        if action == "sec":
            return await self.section(update, arg)
        if action == "skip":
            self.settings.skip(arg)
            return await self._show(update, f"⏸ {S.TITLES[arg]} dilewati. Bisa diisi kapan saja.", kb([BACK]))
        if action == "key":
            sec = S.KEY_SECTIONS[arg]
            self.waiting[chat_id] = {"kind": "key", "section": arg}
            return await self._show(
                update,
                f"🔑 Tempel key {sec.title} di pesan berikutnya.\nAmbil di: {sec.url}\n\n"
                "Pesanmu akan kuhapus segera setelah dibaca.",
                kb([("✖ Batal", "su:menu")]),
            )
        if action == "clear":
            return await self._clear(update, chat_id, arg)
        if action == "pre":
            return await self._preset(update, chat_id, arg)
        if action == "orm":
            st = self.waiting.pop(chat_id, {})
            models = st.get("models") or []
            if not arg.isdigit() or int(arg) >= len(models):
                return await self.menu(update)
            await self._show(update, f"⏳ Mengetes {models[int(arg)]}…")
            ok, msg = await S.add_preset_model(self.settings, _preset("openrouter"), model=models[int(arg)])
            return await self._after_model(update, ok, msg)
        if action == "def":
            if self.settings.get(f"models.list.{arg}"):
                self.settings.set("models.default", arg)
                self.app.reload()
                self.app.agent.llm.set_active(arg)
            return await self.section(update, "model")
        if action == "gaya":
            self.settings.set("style.caveman", arg)
            self.app.reload()
            return await self.section(update, "gaya")
        if action == "shell":
            self.settings.set("shell.enabled", arg == "on")
            self.settings.skip("shell", arg != "on")
            return await self._ask_restart(update, f"Shell {'dinyalakan' if arg == 'on' else 'dimatikan'}.")
        if action == "restart":
            if arg == "yes":
                await self._show(update, "🔄 Restart… aku kabari kalau sudah aktif lagi.")
                self.app.request_restart(chat_id)
            else:
                await self._show(update, "Oke, berlaku setelah restart berikutnya.", kb([BACK]))

    # --- layar per bagian -------------------------------------------------

    async def section(self, update: Update, sid: str) -> None:
        s = self.settings
        feats = {f.id: f for f in await S.status(s)}
        f = feats[sid]
        head = f"{f.icon} {S.TITLES[sid]}" + (f" — {f.detail}" if f.detail else "")

        if sid in S.KEY_SECTIONS:
            sec = S.KEY_SECTIONS[sid]
            has = bool(s.key(sec.key))
            text = f"{head}\n\n{sec.why}\nAmbil key: {sec.url}"
            if sid in ("telegram", "discord"):
                text += "\n\nGanti token = perlu restart."
            rows = [
                [("🔑 Ganti key" if has else "🔑 Isi key", f"su:key:{sid}")],
                [("⏸ Lewati", f"su:skip:{sid}"), ("🗑 Hapus", f"su:clear:{sid}")] if has
                else [("⏸ Lewati", f"su:skip:{sid}")],
            ]
            if sid == "telegram":
                rows = [rows[0]]  # jangan sampai mematikan jalur yang sedang dipakai
            return await self._show(update, text, kb(*rows, [BACK]))

        if sid == "model":
            cfg = self.app.cfg
            lines = [
                f"{'▶' if n == cfg.default_model else '•'} {n} — {m.model}{' 👁' if m.vision else ''}"
                for n, m in cfg.models.items()
            ] or ["(belum ada)"]
            text = (
                f"{head}\n\n" + "\n".join(lines) + "\n\nTambah model (urutan = urutan cadangan). "
                "Model gratis di atas."
            )
            rows = [[(p.label.split(" — ")[0], f"su:pre:{p.id}")] for p in S.PRESETS]
            defaults = [(f"▶ {n}"[:30], f"su:def:{n}") for n in cfg.models if n != cfg.default_model]
            if defaults:
                rows.append(defaults[:3])
            return await self._show(update, text, kb(*rows, [("⏸ Lewati", "su:skip:model"), BACK]))

        if sid == "shell":
            if not self.app.cfg.sandbox_url:
                return await self._show(
                    update,
                    f"{head}\n\nShell butuh mode Docker (sandbox terpisah). Pasang Docker di server, "
                    "lalu jalankan installer lagi.",
                    kb([("⏸ Lewati", "su:skip:shell"), BACK]),
                )
            on = s.get("shell.enabled") is not False
            return await self._show(
                update,
                f"{head}\n\nAgent bisa menjalankan perintah di sandbox. Perintah berisiko tetap minta tombol.",
                kb([("⏸ Matikan" if on else "▶ Nyalakan", f"su:shell:{'off' if on else 'on'}")], [BACK]),
            )

        if sid == "web":
            sites = self.app.cfg.sites
            lines = "\n".join(f"• {x['name']} — {x['path']} (sebagai {x.get('owner') or '?'})" for x in sites)
            return await self._show(
                update,
                f"{head}\n\n{lines or 'Belum ada situs.'}\n\nMenambah situs butuh akses root di server, jadi "
                "hanya bisa dari terminal server:\n  inspire setup web",
                kb([("⏸ Lewati", "su:skip:web"), BACK]),
            )

        if sid == "gaya":
            cur = self.app.cfg.caveman
            opts = [("auto", "🤖 Otomatis"), ("on", "🪨 Caveman"), ("off", "💬 Normal")]
            return await self._show(
                update,
                f"{head}\n\nOtomatis: singkat untuk hal ringan, normal untuk penjelasan.\n"
                "Caveman: selalu sangat singkat (hemat token). Normal: biasa.",
                kb([(("▶ " if m == cur else "") + label, f"su:gaya:{m}") for m, label in opts], [BACK]),
            )

        if sid == "youtube":
            self.waiting[str(update.effective_chat.id)] = {"kind": "cookies"}
            has = bool(self.app.cfg.media.ytdlp_cookies)
            return await self._show(
                update,
                f"{head}\n\nYouTube sering menolak IP server. Solusinya cookies dari browser:\n"
                "1. Pasang ekstensi \"Get cookies.txt LOCALLY\"\n"
                "2. Buka youtube.com (login, sebaiknya akun cadangan)\n"
                "3. Ekspor → kirim file cookies.txt ke sini",
                kb([("🗑 Hapus cookies", "su:clear:youtube")] if has else [],
                   [("⏸ Lewati", "su:skip:youtube"), BACK]),
            )

        if sid == "zona":
            self.waiting[str(update.effective_chat.id)] = {"kind": "zona"}
            return await self._show(
                update,
                f"{head}\n\nKirim nama zona waktu, mis. Asia/Jakarta, Asia/Makassar, Asia/Jayapura.",
                kb([BACK]),
            )

    # --- model ----------------------------------------------------------

    async def _preset(self, update: Update, chat_id: str, pid: str) -> None:
        p = _preset(pid)
        if pid == "custom":
            self.waiting[chat_id] = {"kind": "custom"}
            return await self._show(
                update,
                "Kirim dalam satu pesan, dipisah spasi:\nURL_API NAMA_MODEL [KEY]\n\n"
                "Contoh:\nhttps://opencode.ai/zen/v1 nama-model sk-xxxx\n"
                "http://host.docker.internal:11434/v1 qwen2.5:7b",
                kb([("✖ Batal", "su:menu")]),
            )
        if not self.settings.key(p.key):
            self.waiting[chat_id] = {"kind": "model_key", "preset": pid}
            return await self._show(
                update,
                f"🔑 Tempel key {p.label.split(' — ')[0]} di pesan berikutnya.\nAmbil di: {p.key_url}\n\n"
                "Pesanmu akan kuhapus segera setelah dibaca.",
                kb([("✖ Batal", "su:menu")]),
            )
        await self._model_next(update, chat_id, p)

    async def _model_next(self, update: Update, chat_id: str, p) -> None:
        """Key sudah ada → pilih model (OpenRouter) atau langsung tes."""
        if p.id == "openrouter":
            models = await checks.openrouter_free_models()
            if not models:
                return await self._show(update, "⚠️ Gagal ambil daftar model gratis OpenRouter.", kb([BACK]))
            self.waiting[chat_id] = {"kind": "orm", "models": models}
            rows = [[(m.removesuffix(":free")[-40:], f"su:orm:{i}")] for i, m in enumerate(models)]
            return await self._show(update, "Pilih model gratis (yang mendukung tool):", kb(*rows, [BACK]))
        await self._send(update, f"⏳ Mengetes {p.model}…")
        ok, msg = await S.add_preset_model(self.settings, p)
        await self._after_model(update, ok, msg)

    async def _after_model(self, update: Update, ok: bool, msg: str) -> None:
        if ok:
            self.app.reload()
            await self._send(update, f"✅ {msg}\nLangsung aktif.", kb([("➕ Tambah cadangan", "su:sec:model"), BACK]))
        else:
            prob = S.explain_model_error(msg)
            detail = " ".join(msg.split())[:200]
            await self._send(update, f"❌ Model gagal dites.\n\n{prob.hint}\n\nDetail: {detail}",
                             kb([("↻ Coba lagi", "su:sec:model"), BACK]))

    # --- teks / file masuk ----------------------------------------------------

    async def on_text(self, update: Update) -> None:
        chat_id = str(update.effective_chat.id)
        st = self.waiting.pop(chat_id, None)
        msg = update.effective_message
        text = (msg.text or "").strip()
        if not st:
            return
        secret = st["kind"] in ("key", "model_key", "custom")
        if secret:
            with contextlib.suppress(Exception):
                await msg.delete()  # jangan biarkan key tertinggal di chat

        if st["kind"] == "key":
            sid = st["section"]
            await self._send(update, "⏳ Mengetes…")
            ok, info = await S.save_key(self.settings, sid, text)
            if not ok:
                return await self._send(update, f"❌ {info}. Key tidak disimpan.",
                                        kb([("↻ Coba lagi", f"su:key:{sid}"), BACK]))
            self.app.reload()
            if sid in S.NEEDS_RESTART:
                return await self._ask_restart(update, f"✅ {S.TITLES[sid]}: {info}.")
            return await self._send(update, f"✅ {S.TITLES[sid]} aktif ({info}).", kb([BACK]))

        if st["kind"] == "model_key":
            p = _preset(st["preset"])
            self.settings.set_key(p.key, text)
            return await self._model_next(update, chat_id, p)

        if st["kind"] == "custom":
            parts = text.split()
            if len(parts) < 2:
                return await self._send(update, "Format: URL_API NAMA_MODEL [KEY]", kb([BACK]))
            await self._send(update, f"⏳ Mengetes {parts[1]}…")
            ok, info = await S.add_preset_model(
                self.settings, _preset("custom"), key=parts[2] if len(parts) > 2 else "",
                model=parts[1], api_base=parts[0],
            )
            return await self._after_model(update, ok, info)

        if st["kind"] == "zona":
            try:
                ZoneInfo(text)
            except (ZoneInfoNotFoundError, ValueError):
                self.waiting[chat_id] = st
                return await self._send(update, f"Zona '{text}' tak dikenal. Coba lagi, mis. Asia/Jakarta.")
            self.settings.set("timezone", text)
            return await self._ask_restart(update, f"✅ Zona waktu: {text}.")

        if st["kind"] == "cookies":
            self.waiting[chat_id] = st
            await self._send(update, "Kirim sebagai FILE cookies.txt (bukan teks).")

    async def on_document(self, update: Update, data: bytes) -> None:
        chat_id = str(update.effective_chat.id)
        st = self.waiting.pop(chat_id, None)
        if not st or st["kind"] != "cookies":
            return
        with contextlib.suppress(Exception):
            await update.effective_message.delete()
        if b"youtube.com" not in data and b".google.com" not in data:
            return await self._send(update, "❌ File ini tidak berisi cookies YouTube.", kb([BACK]))
        path = self.settings.dir / "cookies.txt"
        path.write_bytes(data)
        path.chmod(0o600)
        self.settings.set("media.ytdlp_cookies", str(path))
        self.settings.skip("youtube", False)
        self.app.reload()
        await self._send(update, "✅ Cookies YouTube terpasang. Langsung aktif.", kb([BACK]))

    # --- bantu ----------------------------------------------------------

    async def _clear(self, update: Update, chat_id: str, sid: str) -> None:
        if sid in S.KEY_SECTIONS:
            self.settings.set_key(S.KEY_SECTIONS[sid].key, "")
        elif sid == "youtube":
            p = self.settings.get("media.ytdlp_cookies")
            self.settings.set("media.ytdlp_cookies", None)
            if p:
                with contextlib.suppress(OSError):
                    os.remove(p)
        self.app.reload()
        if sid in S.NEEDS_RESTART:
            return await self._ask_restart(update, f"🗑 {S.TITLES[sid]} dihapus.")
        await self._show(update, f"🗑 {S.TITLES[sid]} dihapus.", kb([BACK]))

    async def _ask_restart(self, update: Update, head: str) -> None:
        await self._send(
            update, f"{head}\n\nPerlu restart supaya berlaku. Restart sekarang?",
            kb([("✅ Restart", "su:restart:yes"), ("Nanti", "su:restart:no")]),
        )

    async def _send(self, update: Update, text: str, markup=None) -> None:
        await update.effective_chat.send_message(text, reply_markup=markup, disable_web_page_preview=True)


def _preset(pid: str):
    return next(p for p in S.PRESETS if p.id == pid)
