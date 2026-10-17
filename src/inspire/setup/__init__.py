"""Setup bersama untuk terminal (inspire setup) dan Telegram (/setup).

Satu daftar bagian (SECTIONS), satu cara simpan. Antarmuka hanya bertanya
dan menampilkan; logika ada di sini.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..catalog import resolve
from ..llm_errors import ModelProblem, explain_model_error  # noqa: F401  (dipakai wizard & Telegram)
from ..settings import Settings
from . import checks


CUSTOM_LABEL = "Endpoint sendiri — URL OpenAI-compatible (Ollama, LM Studio, vLLM, server sendiri)"


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


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-")


SHORT = {"openrouter": "or", "google": "gemini", "togetherai": "together", "fireworks-ai": "fireworks"}


def model_name(settings: Settings, provider_id: str, model_id: str) -> str:
    """Nama pendek unik untuk daftar model: or-gemma-4-31b-it, deepseek-flash, …"""
    base = _slug(model_id.split("/")[-1].removesuffix(":free"))
    prefix = SHORT.get(provider_id, provider_id)
    # "deepseek" + "deepseek-flash" → "deepseek-flash", bukan "deepseek-deepseek-flash".
    name = (base if base.startswith(prefix) else f"{prefix}-{base}")[:32].rstrip("-")
    existing = settings.get("models.list") or {}
    final, n = name, 2
    while final in existing:
        final, n = f"{name}-{n}", n + 1
    return final


async def add_model_spec(
    settings: Settings, name: str, spec: dict, key_env: str = "", key: str = "",
    make_default: bool = True,
) -> tuple[bool, str]:
    """Tes model SUNGGUHAN, baru simpan. Key baru hanya disimpan kalau tes lolos (key salah
    tak boleh tertinggal). key kosong = pakai yang sudah tersimpan. spec["api_key"] boleh
    berupa "${ENV}" (disimpan seperti itu, dites dengan nilai aslinya)."""
    key = key.strip()
    shown = key or (settings.key(key_env) if key_env else "")
    test = dict(spec)
    if "api_key" in spec:
        if spec["api_key"].startswith("${"):
            if not shown:
                return False, f"key {key_env} belum diisi (401 api key kosong)"
            test["api_key"] = shown
    elif key_env:
        test["api_key"] = shown
    ok, msg = await checks.model(test)
    if not ok:
        return False, msg
    if key and key_env:
        settings.set_key(key_env, key)
    settings.add_model(name, spec, make_default)
    settings.skip("model", False)
    return True, f"{name} siap ({msg})"


async def add_catalog_model(
    settings: Settings, provider, model, key: str = "", make_default: bool = True,
) -> tuple[bool, str]:
    """Model dari katalog models.dev (lihat catalog.py)."""
    res = resolve(provider, model)
    if not res:
        return False, f"penyedia {provider.name} belum didukung"
    spec: dict = {"model": res.model, "api_key": "${" + res.key_env + "}" if res.key_required else "none"}
    if res.api_base:
        spec["api_base"] = res.api_base
    if model.vision:
        spec["vision"] = True
    if model.context:
        spec["context"] = model.context
    if model.cost_in is not None and model.cost_out is not None:
        spec["cost_in"], spec["cost_out"] = model.cost_in, model.cost_out
    return await add_model_spec(
        settings, model_name(settings, provider.id, model.id), spec,
        res.key_env if res.key_required else "", key, make_default)


async def add_custom_model(
    settings: Settings, model: str, api_base: str, key: str = "", make_default: bool = True,
) -> tuple[bool, str]:
    """Endpoint OpenAI-compatible buatan sendiri (Ollama, LM Studio, vLLM, …)."""
    name = "custom-" + _slug(model.split("/")[-1])[:24]
    spec = {"model": "openai/" + model.removeprefix("openai/"), "api_base": api_base}
    key = key.strip()
    if key:
        env = "CUSTOM_" + re.sub(r"[^A-Z0-9]", "_", name.upper())
        spec["api_key"] = "${" + env + "}"
        return await add_model_spec(settings, name, spec, env, key, make_default)
    spec["api_key"] = "none"  # server lokal tanpa key; litellm tetap butuh nilai
    return await add_model_spec(settings, name, spec, "", "", make_default)


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
