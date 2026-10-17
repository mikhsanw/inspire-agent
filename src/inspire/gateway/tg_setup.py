"""/setup di Telegram: menu tombol per fitur, tempel key, lewati, hapus.

Pesan berisi key langsung dihapus setelah dibaca.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup, Update

from .. import catalog as catalog_mod
from .. import setup as S

log = logging.getLogger(__name__)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn(t, callback_data=d) for t, d in row] for row in rows if row])


WAIT_TTL = 600  # detik; setelah itu teks biasa kembali jadi obrolan, bukan jawaban /setup


class Waiting(dict):
    """chat_id → status yang sedang ditunggu. Tiap penetapan menyegarkan waktunya."""

    def __setitem__(self, key, value):
        value["ts"] = time.time()
        super().__setitem__(key, value)


BACK = ("⬅ Menu", "su:menu")
PAGE = 8


class TelegramSetup:
    def __init__(self, app):
        self.app = app
        # chat_id → apa yang sedang ditunggu: {"kind": ..., ...}
        self.waiting: Waiting = Waiting()
        self._catalog = None

    @property
    def settings(self):
        return self.app.settings

    def is_waiting(self, chat_id: str) -> bool:
        st = self.waiting.get(chat_id)
        if st and time.time() - st.get("ts", 0) > WAIT_TTL:
            self.waiting.pop(chat_id, None)
            return False
        return st is not None

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
        if action == "prov":
            return await self._provider(update, chat_id, arg)
        if action == "provq":
            self.waiting[chat_id] = {"kind": "provq"}
            return await self._show(update, "🔎 Ketik nama penyedia (mis. together, nvidia, fireworks, huggingface).",
                                    kb([("⬅ Penyedia", "su:sec:model"), BACK]))
        if action == "custom":
            self.waiting[chat_id] = {"kind": "custom"}
            return await self._show(
                update,
                "Kirim dalam satu pesan, dipisah spasi:\nURL_API NAMA_MODEL [KEY]\n\nContoh:\n"
                "http://host.docker.internal:11434/v1 qwen2.5:7b\nhttps://api.contoh.com/v1 nama-model sk-xxxx",
                kb([("⬅ Penyedia", "su:sec:model"), BACK]),
            )
        if action in ("mdl", "mf", "ms", "mp", "ml", "rekey"):
            return await self._picker_action(update, chat_id, action, arg)
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
            cat = await self._cat()
            rows = []
            if cat:
                pops = [(p.name[:22], f"su:prov:{p.id}") for p in cat.popular()]
                rows = [pops[i:i + 2] for i in range(0, len(pops), 2)]
                rows.append([("🔎 Cari penyedia", "su:provq"), ("🔧 Endpoint sendiri", "su:custom")])
            else:
                text += "\n\n⚠️ Katalog model tak bisa diambil (koneksi?). Pakai endpoint sendiri dulu."
                rows = [[("🔧 Endpoint sendiri", "su:custom")]]
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

    async def _cat(self):
        if self._catalog is None or self._catalog.age_hours() > 24:
            self._catalog = await catalog_mod.load(self.settings.dir / "catalog.json")
        return self._catalog

    async def _provider(self, update: Update, chat_id: str, pid: str) -> None:
        cat = await self._cat()
        prov = cat and cat.get(pid)
        if not prov:
            return await self._show(update, "Penyedia tak ditemukan.", kb([BACK]))
        if prov.key_required and not self.settings.key(prov.key_env):
            self.waiting[chat_id] = {"kind": "model_key", "prov": pid}
            url = catalog_mod.KEY_URL.get(pid, f"situs {prov.name}")
            return await self._show(
                update, f"🔑 Tempel key {prov.name} di pesan berikutnya.\nAmbil di: {url}\n\n"
                "Pesanmu akan kuhapus segera setelah dibaca.", kb([("⬅ Penyedia", "su:sec:model"), BACK]))
        await self._model_list(update, chat_id, {"kind": "catalog", "prov": pid, "key": "", "query": "",
                                                 "free": False, "sort": "baru", "page": 0})

    async def _model_list(self, update: Update, chat_id: str, st: dict) -> None:
        cat = await self._cat()
        prov = cat.get(st["prov"])
        items = cat.models(st["prov"], st["query"], st["free"], st["sort"])
        pages = max(1, -(-len(items) // PAGE))
        st["page"] = min(st["page"], pages - 1)
        start = st["page"] * PAGE
        view = items[start:start + PAGE]
        head = (f"{prov.name} · {len(items)} model · urut {catalog_mod.SORTS[st['sort']]}"
                + (" · hanya $0" if st["free"] else "") + (f" · cari '{st['query']}'" if st["query"] else ""))
        lines = [f"{start + i + 1}. {m.label()}" for i, m in enumerate(view)] or ["(tak ada yang cocok)"]
        text = (f"{head}\n\n" + "\n".join(lines) +
                "\n\nKetik kata untuk mencari. Harga = $ per 1 juta token (input/output) · 👁 bisa lihat gambar · "
                "$0 menurut katalog, syarat tier gratis ditentukan penyedia.")
        nums = [(str(start + i + 1), f"su:mdl:{start + i}") for i in range(len(view))]
        rows = [nums[i:i + 4] for i in range(0, len(nums), 4)]
        rows.append([("🆓 $0 ✓" if st["free"] else "🆓 $0", "su:mf:x"), (f"↕ {catalog_mod.SORTS[st['sort']]}", "su:ms:x")])
        if pages > 1:
            rows.append([("⬅", f"su:mp:{(st['page'] - 1) % pages}"), (f"{st['page'] + 1}/{pages}", f"su:mp:{st['page']}"),
                         ("➡", f"su:mp:{(st['page'] + 1) % pages}")])
        rows.append([("⬅ Penyedia", "su:sec:model"), BACK])
        self.waiting[chat_id] = st
        await self._show(update, text, kb(*rows))

    async def _picker_action(self, update: Update, chat_id: str, action: str, arg: str) -> None:
        st = self.waiting.get(chat_id)
        if not st or st.get("kind") != "catalog":
            return await self.menu(update)
        if action == "mf":
            st["free"], st["page"] = not st["free"], 0
        elif action == "ms":
            order = list(catalog_mod.SORTS)
            st["sort"], st["page"] = order[(order.index(st["sort"]) + 1) % len(order)], 0
        elif action == "mp" and arg.isdigit():
            st["page"] = int(arg)
        elif action == "rekey":
            self.waiting[chat_id] = {"kind": "model_key", "state": st}
            return await self._show(update, "🔑 Tempel ulang key-nya. Pesanmu akan kuhapus segera.",
                                    kb([("⬅ Penyedia", "su:sec:model"), BACK]))
        elif action == "mdl" and arg.isdigit():
            return await self._try_model(update, chat_id, st, int(arg))
        await self._model_list(update, chat_id, st)

    async def _try_model(self, update: Update, chat_id: str, st: dict, idx: int) -> None:
        cat = await self._cat()
        prov = cat.get(st["prov"])
        items = cat.models(st["prov"], st["query"], st["free"], st["sort"])
        if idx >= len(items):
            return await self._model_list(update, chat_id, st)
        model = items[idx]
        await self._show(update, f"⏳ Mengetes {model.id}…")
        ok, msg = await S.add_catalog_model(self.settings, prov, model, key=st.get("key", ""))
        if ok:
            self.waiting.pop(chat_id, None)
            self.app.reload()
            return await self._send(update, f"✅ {msg}\nLangsung aktif.",
                                    kb([("➕ Tambah cadangan", "su:sec:model"), BACK]))
        st["retry"] = idx
        self.waiting[chat_id] = st
        prob = S.explain_model_error(msg)
        detail = " ".join(msg.split())[:200]
        buttons = []
        if prob.field == "key":
            buttons.append(("🔑 Tempel ulang key", "su:rekey:x"))
        if prob.field in ("model", "tools", "context", "other"):
            buttons.append(("↩ Pilih model lain", "su:ml:x"))
        await self._send(update, f"❌ {model.id} gagal dites.\n\n{prob.hint}\n\nDetail: {detail}",
                         kb(buttons, [("⬅ Penyedia lain", "su:sec:model"), BACK]))

    async def _after_custom(self, update: Update, ok: bool, msg: str) -> None:
        if ok:
            self.app.reload()
            return await self._send(update, f"✅ {msg}\nLangsung aktif.",
                                    kb([("➕ Tambah cadangan", "su:sec:model"), BACK]))
        prob = S.explain_model_error(msg, "custom")
        detail = " ".join(msg.split())[:200]
        await self._send(update, f"❌ Gagal dites.\n\n{prob.hint}\n\nDetail: {detail}",
                         kb([("↻ Coba lagi", "su:custom"), BACK]))

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
            # Key hanya dipegang di memori sampai tes model lolos (baru disimpan).
            if st.get("state"):  # ulang key setelah gagal → coba lagi model yang sama
                state = st["state"]
                state["key"] = text
                self.waiting[chat_id] = state
                return await self._try_model(update, chat_id, state, state.get("retry", 0))
            return await self._model_list(update, chat_id, {
                "kind": "catalog", "prov": st["prov"], "key": text, "query": "", "free": False,
                "sort": "baru", "page": 0})

        if st["kind"] == "catalog":  # teks biasa = kata pencarian model
            st["query"], st["page"] = ("" if text == "-" else text), 0
            return await self._model_list(update, chat_id, st)

        if st["kind"] == "provq":
            cat = await self._cat()
            found = cat.search_providers(text)[:10] if cat else []
            if not found:
                self.waiting[chat_id] = st
                return await self._send(update, f"Tak ada penyedia '{text}'. {catalog_mod.HIDDEN_NOTE}")
            return await self._send(update, "Pilih penyedia:",
                                    kb(*[[(f"{p.name[:30]}", f"su:prov:{p.id}")] for p in found], [BACK]))

        if st["kind"] == "custom":
            parts = text.split()
            if len(parts) < 2:
                self.waiting[chat_id] = st
                return await self._send(update, "Format: URL_API NAMA_MODEL [KEY]", kb([BACK]))
            await self._send(update, f"⏳ Mengetes {parts[1]}…")
            ok, info = await S.add_custom_model(
                self.settings, parts[1], parts[0], key=parts[2] if len(parts) > 2 else "")
            return await self._after_custom(update, ok, info)

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
