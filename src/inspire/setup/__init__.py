"""Setup bersama untuk terminal (inspire setup) dan Telegram (/setup).

Satu daftar bagian (SECTIONS), satu cara simpan. Antarmuka hanya bertanya
dan menampilkan; logika ada di sini.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..settings import Settings
from . import checks


@dataclass
class Preset:
    id: str
    label: str
    key: str = ""             # nama env key-nya
    key_url: str = ""
    model: str = ""           # kosong = dipilih belakangan (openrouter) / diisi user (custom)
    vision: bool = False


PRESETS = [
    Preset("openrouter", "OpenRouter — model gratis (daftar akun, tanpa kartu)",
           "OPENROUTER_API_KEY", "https://openrouter.ai/keys"),
    Preset("gemini", "Google Gemini Flash — gratis, bisa lihat gambar",
           "GEMINI_API_KEY", "https://aistudio.google.com/apikey", "gemini/gemini-2.5-flash", True),
    Preset("deepseek", "DeepSeek — murah & stabil (top-up ~$2)",
           "DEEPSEEK_API_KEY", "https://platform.deepseek.com/api_keys", "deepseek/deepseek-chat"),
    Preset("custom", "Lainnya — endpoint OpenAI-compatible (OpenCode Zen, Ollama, …)"),
]


@dataclass
class KeySection:
    """Bagian yang isinya cuma satu key: tempel → tes → simpan → langsung aktif."""

    id: str
    title: str
    why: str
    key: str
    url: str
    check: Callable[[str], Awaitable[tuple[bool, str]]]
    restart: bool = False


KEY_SECTIONS = {
    "telegram": KeySection(
        "telegram", "Telegram", "Ngobrol dengan agent lewat Telegram.",
        "TELEGRAM_BOT_TOKEN", "https://t.me/BotFather → /newbot", checks.telegram, restart=True),
    "discord": KeySection(
        "discord", "Discord", "Ngobrol lewat DM Discord. Aktifkan Message Content Intent.",
        "DISCORD_BOT_TOKEN", "https://discord.com/developers/applications → Bot → Reset Token",
        checks.discord, restart=True),
    "suara": KeySection(
        "suara", "Suara", "Voice note jadi teks (Groq Whisper, gratis).",
        "GROQ_API_KEY", "https://console.groq.com/keys", checks.groq),
    "gambar": KeySection(
        "gambar", "Gambar", "Deskripsi gambar untuk model yang tak bisa melihat (Gemini, gratis).",
        "GEMINI_API_KEY", "https://aistudio.google.com/apikey", checks.gemini),
}

# Urutan tampil. "model", "shell", "youtube", "zona" punya alur sendiri.
ORDER = ["telegram", "model", "suara", "gambar", "discord", "shell", "web", "gaya", "youtube", "zona"]
TITLES = {
    "telegram": "Telegram", "model": "Model AI", "suara": "Suara", "gambar": "Gambar",
    "discord": "Discord", "shell": "Shell & coding", "web": "Web di server", "gaya": "Gaya jawaban",
    "youtube": "YouTube", "zona": "Zona waktu",
}
NEEDS_RESTART = {"telegram", "discord", "shell"}


async def save_key(settings: Settings, section: str, value: str) -> tuple[bool, str]:
    sec = KEY_SECTIONS[section]
    value = value.strip()
    ok, msg = await sec.check(value)
    if not ok:
        return False, msg
    settings.set_key(sec.key, value)
    settings.skip(section, False)
    return True, msg


async def add_preset_model(
    settings: Settings, preset: Preset, key: str = "", model: str = "",
    api_base: str = "", make_default: bool = True,
) -> tuple[bool, str]:
    """Simpan key (kalau ada), tes model, daftarkan. key kosong = pakai yang tersimpan."""
    if preset.key and key:
        settings.set_key(preset.key, key.strip())
    name = preset.id
    spec: dict = {"model": model or preset.model}
    if preset.id == "openrouter":
        spec["model"] = "openrouter/" + model.removeprefix("openrouter/")
        name = "or-" + model.split("/")[-1].removesuffix(":free")[:24]
    if preset.id == "custom":
        name = "custom-" + model.split("/")[-1][:24]
        spec = {"model": "openai/" + model.removeprefix("openai/"), "api_base": api_base}
        if key:
            env = "CUSTOM_" + "".join(c if c.isalnum() else "_" for c in name.upper())
            settings.set_key(env, key.strip())
            spec["api_key"] = "${" + env + "}"
    if preset.vision:
        spec["vision"] = True
    test = dict(spec)
    if "api_key" in test:
        test["api_key"] = key.strip()
    ok, msg = await checks.model(test)
    if not ok:
        return False, msg
    settings.add_model(name, spec, make_default)
    settings.skip("model", False)
    return True, f"{name} siap ({msg})"


@dataclass
class Feature:
    id: str
    title: str
    state: str          # "on" | "skip" | "off" | "err"
    detail: str = ""
    fix: str = ""

    @property
    def icon(self) -> str:
        return {"on": "✅", "skip": "⏸", "off": "⬜", "err": "❌"}[self.state]


async def status(settings: Settings, live: bool = False) -> list[Feature]:
    """Keadaan tiap fitur. live=True ikut tes sandbox (cepat)."""
    from ..config import from_settings

    cfg = from_settings(settings)
    out: list[Feature] = []

    def add(fid, state, detail="", fix=""):
        if state != "on" and settings.is_skipped(fid):
            state = "skip"
        out.append(Feature(fid, TITLES[fid], state, detail, fix))

    tg_owner = cfg.allowed_user_ids
    if cfg.telegram_token and tg_owner:
        add("telegram", "on", f"pemilik: {', '.join(map(str, tg_owner))}")
    elif cfg.telegram_token:
        add("telegram", "err", "token ada, belum di-pair",
            f"kirim /pair {settings.pairing_code()} ke bot")
    else:
        add("telegram", "off", "", "inspire setup telegram")

    if cfg.models:
        add("model", "on", f"utama: {cfg.default_model} ({cfg.models[cfg.default_model].model}), "
                           f"total {len(cfg.models)} model")
    else:
        add("model", "err", "belum ada model — bot tak bisa menjawab", "inspire setup model / /setup")

    add("suara", "on" if cfg.media.stt_api_key else "off", "Groq Whisper" if cfg.media.stt_api_key else "",
        "inspire setup suara")
    vision_main = any(m.vision for m in cfg.models.values())
    if settings.key("GEMINI_API_KEY") or vision_main:
        add("gambar", "on", "model vision" if vision_main else "deskripsi lewat Gemini")
    else:
        add("gambar", "off", "", "inspire setup gambar")

    if cfg.discord_token and cfg.discord_user_ids:
        add("discord", "on", f"pemilik: {', '.join(map(str, cfg.discord_user_ids))}")
    elif cfg.discord_token:
        add("discord", "err", "token ada, belum di-pair",
            f"DM bot: !pair {settings.pairing_code()}")
    else:
        add("discord", "off", "", "inspire setup discord")

    if not cfg.sandbox_url:
        add("shell", "off", "butuh mode Docker", "pasang Docker lalu install ulang")
    elif settings.get("shell.enabled") is False:
        add("shell", "skip", "dimatikan")
    elif live:
        ok, msg = await checks.sandbox(cfg.sandbox_url, cfg.sandbox_token)
        add("shell", "on" if ok else "err", msg, "" if ok else "docker compose up -d sandbox")
    else:
        add("shell", "on", "sandbox Docker")

    if cfg.sites and all(x.get("owner") for x in cfg.sites):
        add("web", "on", ", ".join(x["name"] for x in cfg.sites))
    elif cfg.sites:
        add("web", "err", "situs dicatat, langkah sudo belum selesai", "inspire setup web (di server)")
    else:
        add("web", "off", "", "inspire setup web (di server)")
    add("gaya", "on", {"auto": "otomatis", "on": "caveman", "off": "normal"}.get(cfg.caveman, cfg.caveman))

    cookies = cfg.media.ytdlp_cookies
    add("youtube", "on" if cookies else "off",
        "cookies terpasang" if cookies else "tanpa cookies (YouTube bisa menolak IP server)",
        "kirim cookies.txt lewat /setup")
    add("zona", "on", cfg.timezone)
    return out


def render(features: list[Feature]) -> str:
    lines = []
    for f in features:
        line = f"{f.icon} {f.title}"
        if f.detail:
            line += f" — {f.detail}"
        if f.state in ("off", "err") and f.fix:
            line += f"\n    ↳ {f.fix}"
        lines.append(line)
    return "\n".join(lines)
