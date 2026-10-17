"""Wizard terminal: `inspire setup [bagian]`. Enter = lewati."""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .. import setup as S
from ..settings import Settings
from . import checks

TTY = sys.stdout.isatty()


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if TTY else text


def say(text: str = "") -> None:
    print(text, flush=True)


def ok(text: str) -> None:
    say(c("✅ " + text, "32"))


def bad(text: str) -> None:
    say(c("❌ " + text, "31"))


def head(text: str) -> None:
    say()
    say(c(f"── {text} ", "1;36") + c("─" * max(0, 50 - len(text)), "2"))


def ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{hint}: ").strip()
    except EOFError:
        val = ""
    return val or default


def ask_secret(prompt: str) -> str:
    try:
        return getpass.getpass(f"{prompt} (tak terlihat saat ditempel, Enter = lewati): ").strip()
    except EOFError:
        return ""


def yes(prompt: str, default: bool = True) -> bool:
    ans = ask(f"{prompt} {'[Y/n]' if default else '[y/N]'}").lower()
    return default if not ans else ans.startswith("y")


def choose(prompt: str, options: list[str], default: int = 1, allow_skip: bool = True) -> int | None:
    for i, o in enumerate(options, 1):
        say(f"  {i}) {o}")
    if allow_skip:
        say("  0) Lewati")
    while True:
        ans = ask(prompt, str(default))
        if ans.isdigit() and (int(ans) == 0 and allow_skip):
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return int(ans) - 1
        say("Pilih nomor dari daftar.")


def run(coro):
    return asyncio.run(coro)


# --- langkah ----------------------------------------------------------------


def step_key(s: Settings, sid: str) -> bool:
    sec = S.KEY_SECTIONS[sid]
    head(sec.title)
    say(sec.why)
    if s.key(sec.key):
        say(f"Sudah ada key {sec.title}.")
        if not yes("Ganti?", False):
            return True
    say(f"Ambil di: {c(sec.url, '4')}")
    while True:
        val = ask_secret(f"Tempel {sec.key}")
        if not val:
            s.skip(sid)
            say(c("⏸ dilewati — isi nanti: inspire setup " + sid + " / Telegram /setup", "33"))
            return False
        res, msg = run(S.save_key(s, sid, val))
        if res:
            ok(msg)
            return True
        bad(msg)


def step_pair_telegram(s: Settings) -> None:
    if s.owners("telegram"):
        ok(f"Telegram sudah punya pemilik ({', '.join(map(str, s.owners('telegram')))}).")
        return
    token = s.key("TELEGRAM_BOT_TOKEN")
    good, username = run(checks.telegram(token))
    if not good:
        return
    code = s.pairing_code()
    head("Pairing")
    say("Supaya bot hanya menurut padamu. Di HP, buka:")
    say("  " + c(f"https://t.me/{username.lstrip('@')}?start={code}", "1;4"))
    say(f"lalu tekan START. (Atau kirim: /pair {code})")
    say(c("Menunggu… (Ctrl+C = lewati, pairing bisa nanti)", "2"))
    try:
        user = run(_wait_pair(token, code))
    except KeyboardInterrupt:
        say()
        say(c(f"⏸ Pairing dilewati. Nanti kirim /pair {code} ke bot.", "33"))
        return
    if user is None:
        say(c(f"Bot sedang jalan di tempat lain. Kirim /pair {code} langsung ke bot.", "33"))
        return
    s.add_owner("telegram", user["id"])
    ok(f"Terhubung dengan {user.get('first_name', '')} (@{user.get('username', '-')}).")


async def _wait_pair(token: str, code: str, minutes: int = 10) -> dict | None:
    """Baca getUpdates sampai ada '/start CODE' atau '/pair CODE'. None = bentrok (409)."""
    api = f"https://api.telegram.org/bot{token}"
    offset = 0
    async with httpx.AsyncClient(timeout=40) as client:
        for _ in range(minutes * 2):
            r = await client.get(f"{api}/getUpdates", params={"timeout": 30, "offset": offset})
            if r.status_code == 409:
                return None
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                words = (msg.get("text") or "").split()
                if words[:1] in (["/start"], ["/pair"]) and words[1:2] == [code]:
                    await client.get(f"{api}/getUpdates", params={"offset": offset, "timeout": 0})
                    await client.post(f"{api}/sendMessage", json={
                        "chat_id": msg["chat"]["id"],
                        "text": "✅ Terhubung! Kamu pemilik bot ini. Selesaikan setup di terminal, "
                                "atau nanti ketik /setup di sini.",
                    })
                    return msg["from"]
    raise KeyboardInterrupt  # kelamaan → anggap dilewati


def step_model(s: Settings) -> bool:
    head("Model AI")
    models = (s.get("models.list") or {})
    if models:
        say("Sudah ada: " + ", ".join(models))
        if not yes("Tambah model lain?", False):
            return True
    say("Wajib minimal satu, supaya bot bisa menjawab. Yang gratis di atas.")
    while True:
        idx = choose("Pilih", [p.label for p in S.PRESETS])
        if idx is None:
            s.skip("model")
            say(c("⏸ dilewati — bot belum bisa menjawab sampai model diisi.", "33"))
            return False
        p = S.PRESETS[idx]
        key, model, base = "", "", ""
        if p.id == "custom":
            base = ask("URL API (OpenAI-compatible), mis. https://opencode.ai/zen/v1")
            model = ask("Nama model")
            key = ask_secret("API key (kosongkan kalau tak perlu)")
            if not base or not model:
                continue
        else:
            if not s.key(p.key) or yes(f"Ganti key {p.key}?", False):
                say(f"Ambil key di: {c(p.key_url, '4')}")
                key = ask_secret(f"Tempel {p.key}")
                if not key and not s.key(p.key):
                    continue
        if p.id == "openrouter":
            say("Mengambil daftar model gratis yang mendukung tool…")
            free = run(checks.openrouter_free_models())
            if not free:
                bad("Gagal ambil daftar. Coba lagi nanti.")
                continue
            i = choose("Pilih model", [m.removesuffix(":free") for m in free], allow_skip=False)
            model = free[i]
        say("Mengetes model…")
        res, msg = run(S.add_preset_model(s, p, key=key, model=model, api_base=base))
        if res:
            ok(msg)
            if not yes("Tambah model cadangan (dipakai kalau yang utama error/limit)?", False):
                return True
        else:
            bad(msg)


def step_shell(s: Settings) -> None:
    head("Shell")
    if not os.environ.get("SANDBOX_URL"):
        say("Shell butuh mode Docker (sandbox terpisah). Mode ini tanpa Docker → dilewati.")
        s.skip("shell")
        return
    on = yes("Izinkan agent menjalankan perintah di sandbox? (perintah berisiko tetap minta tombol)")
    s.set("shell.enabled", on)
    s.skip("shell", not on)
    ok("Shell " + ("aktif" if on else "mati"))


def step_youtube(s: Settings) -> None:
    head("YouTube (opsional)")
    say("YouTube sering menolak IP server. Kalau punya cookies.txt dari browser, masukkan path-nya.")
    say("Bisa juga nanti: kirim file cookies.txt lewat Telegram /setup.")
    path = ask("Path cookies.txt (Enter = lewati)")
    if not path:
        s.skip("youtube")
        return
    src = Path(path).expanduser()
    if not src.exists():
        bad("File tak ada.")
        return
    dst = s.dir / "cookies.txt"
    dst.write_bytes(src.read_bytes())
    dst.chmod(0o600)
    s.set("media.ytdlp_cookies", str(dst))
    s.skip("youtube", False)
    ok("Cookies terpasang.")


def step_zona(s: Settings) -> None:
    head("Zona waktu")
    while True:
        tz = ask("Zona waktu", s.get("timezone") or "Asia/Jakarta")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            bad("Tak dikenal. Contoh: Asia/Jakarta, Asia/Makassar, Asia/Jayapura")
            continue
        s.set("timezone", tz)
        return


def step_discord(s: Settings) -> None:
    if step_key(s, "discord") and not s.owners("discord"):
        say(f"Setelah bot jalan, DM bot Discord-mu: {c('!pair ' + s.pairing_code(), '1')}")


def step_telegram(s: Settings) -> bool:
    if not step_key(s, "telegram"):
        return False
    step_pair_telegram(s)
    return True


STEPS = {
    "telegram": step_telegram,
    "pair": step_pair_telegram,
    "model": step_model,
    "suara": lambda s: step_key(s, "suara"),
    "gambar": lambda s: step_key(s, "gambar"),
    "discord": step_discord,
    "shell": step_shell,
    "youtube": step_youtube,
    "zona": step_zona,
}


def wizard(s: Settings, section: str | None = None) -> None:
    try:
        if section:
            if section not in STEPS:
                bad(f"Bagian tak dikenal. Pilih: {', '.join(STEPS)}")
                return
            STEPS[section](s)
        else:
            say(c("inspire setup", "1;36") + c("  — Enter = lewati, semua bisa diisi nanti", "2"))
            mode = choose("Mode", [
                "Cepat — Telegram + pairing + 1 model (±2 menit)",
                "Lengkap — semua fitur (suara, gambar, Discord, shell, YouTube, zona)",
            ], allow_skip=False)
            if not STEPS["telegram"](s) and mode == 0:
                say("Tanpa Telegram, bisa pakai Discord.")
                step_discord(s)
            step_model(s)
            if mode == 1:
                for sid in ("suara", "gambar", "discord", "shell", "youtube", "zona"):
                    STEPS[sid](s)
    except KeyboardInterrupt:
        say()
        say(c("Dihentikan. Yang sudah diisi tetap tersimpan.", "33"))
    summary(s)


def summary(s: Settings) -> None:
    head("Status")
    say(S.render(run(S.status(s, live=True))))
    say()
    if os.environ.get("INSPIRE_MODE") in ("docker", "native"):
        say("Ubah kapan saja: " + c("inspire setup <bagian>", "1") + " atau " + c("/setup", "1") + " di Telegram.")
    else:
        say("Jalankan bot: " + c("inspire run", "1") + "   Ubah setelan: " + c("inspire setup <bagian>", "1"))


def doctor(s: Settings) -> int:
    """Status + tes sungguhan semua key yang terisi. Return 1 kalau ada error."""
    head("Status fitur")
    feats = run(S.status(s, live=True))
    say(S.render(feats))
    head("Tes key")
    tests = []
    for sid, sec in S.KEY_SECTIONS.items():
        if s.key(sec.key):
            tests.append((sec.title, sec.check(s.key(sec.key))))
    from ..config import from_settings

    cfg = from_settings(s)
    if cfg.default_model:
        m = cfg.models[cfg.default_model]
        tests.append((f"Model {cfg.default_model}", checks.model(
            {"model": m.model, "api_base": m.api_base, "api_key": m.api_key})))

    async def all_tests():
        return await asyncio.gather(*(t for _, t in tests))

    failed = False
    for (title, _), (good, msg) in zip(tests, run(all_tests()) if tests else []):
        (ok if good else bad)(f"{title}: {msg}")
        failed |= not good
    if not tests:
        say("(belum ada key)")
    return 1 if failed or any(f.state == "err" for f in feats) else 0
