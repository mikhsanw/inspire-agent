"""Ubah pohon setelan (dari settings.json / config.yaml) jadi Config. ${VAR} diisi dari env."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .media import MediaCfg

_ENV = re.compile(r"\$\{(\w+)\}")


def _expand(value):
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


@dataclass
class ModelCfg:
    model: str                    # nama litellm, mis. "deepseek/deepseek-chat"
    api_base: str | None = None   # untuk endpoint OpenAI-compatible (OpenCode Zen, dll)
    api_key: str | None = None    # biasanya "${NAMA_ENV}"; kosong = litellm baca env sendiri
    vision: bool = False          # bisa melihat gambar → gambar dikirim langsung
    context: int = 0              # ukuran konteks model (token); 0 = tak diketahui
    timeout: float = 0            # detik per panggilan; 0 = pakai agent.request_timeout
    cost_in: float | None = None  # dolar per 1 juta token (info katalog, untuk tampilan)
    cost_out: float | None = None
    extra: dict = field(default_factory=dict)  # diteruskan ke litellm apa adanya


def describe(m: "ModelCfg") -> str:
    """Ringkasan satu baris untuk /model: nama provider/model · konteks · harga · 👁."""
    parts = [m.model]
    if m.context:
        parts.append(f"{m.context // 1000}k")
    if m.cost_in is not None and m.cost_out is not None:
        parts.append("$0" if m.cost_in == 0 and m.cost_out == 0 else f"${m.cost_in:g}/${m.cost_out:g}")
    if m.vision:
        parts.append("👁")
    return " · ".join(parts)


@dataclass
class Config:
    data_dir: Path
    telegram_token: str
    allowed_user_ids: list[int]
    models: dict[str, ModelCfg]
    default_model: str
    fallback: list[str]
    discord_token: str = ""
    discord_user_ids: list[int] = field(default_factory=list)
    discord_prefix: str = "!"
    caveman: str = "auto"         # auto | on | off
    host_address: str = ""        # SSH ke server web; kosong = fitur web mati
    host_port: int = 22
    host_user: str = "inspire"
    host_key: str = ""            # path private key
    sites: list = field(default_factory=list)  # [{name, path, owner}]
    memory_chars: int = 2200
    user_chars: int = 1400
    max_context_tokens: int = 48000
    compress_at: float = 0.75
    keep_last: int = 12
    idle_hours: float = 6
    max_iterations: int = 12
    task_max_iterations: int = 60
    task_max_minutes: int = 30
    task_progress_seconds: int = 45
    request_timeout: float = 75
    chain_timeout: float = 150
    timezone: str = "Asia/Jakarta"
    sandbox_url: str = ""         # kosong = tool shell mati
    sandbox_token: str = ""
    shell_timeout: int = 60
    confirm_timeout: int = 300    # detik menunggu tombol ✅/❌
    fetch_max_chars: int = 12000
    search_backends: list[str] = field(default_factory=lambda: ["duckduckgo", "bing", "yahoo"])
    media: MediaCfg = field(default_factory=MediaCfg)
    inbox_dir: Path | None = None  # salinan file kiriman user, terlihat di sandbox


def _ids(raw) -> list[int]:
    """Boleh list YAML [1, 2] atau string "1,2" (mis. dari ${ALLOWED_USER_IDS})."""
    if not raw:
        return []
    if not isinstance(raw, list):
        raw = str(raw).replace(",", " ").split()
    return [int(x) for x in raw if str(x).strip()]


def load(path: Path | str) -> Config:
    """Dari satu file YAML (cara lama, dipakai test & config lanjutan)."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return from_raw(raw, Path(raw.get("data_dir", "data")))


def from_settings(settings) -> Config:
    return from_raw(settings.raw(), settings.dir)


def from_raw(raw: dict, data_dir: Path) -> Config:
    raw = _expand(raw)
    tg = raw.get("telegram", {})
    m = raw.get("models", {})
    mem = raw.get("memory", {})
    ctx = raw.get("context", {})
    agent = raw.get("agent", {})
    shell = raw.get("shell", {})
    web = raw.get("web", {})
    md = raw.get("media", {})
    stt = md.get("stt", {})

    # Boleh kosong: bot tetap hidup dan mengarahkan ke /setup.
    models = {name: ModelCfg(**spec) for name, spec in (m.get("list") or {}).items()}
    default = m.get("default") if m.get("default") in models else next(iter(models), "")
    fallback = [n for n in (m.get("fallback") or []) if n in models]

    dc = raw.get("discord", {})
    host = raw.get("host", {})

    return Config(
        data_dir=data_dir,
        telegram_token=tg.get("token", ""),
        allowed_user_ids=_ids(tg.get("allowed_user_ids")),
        discord_token=dc.get("token", ""),
        discord_user_ids=_ids(dc.get("allowed_user_ids")),
        discord_prefix=dc.get("command_prefix", "!"),
        caveman=str((raw.get("style") or {}).get("caveman", "auto")),
        host_address=host.get("address", ""),
        host_port=int(host.get("port", 22)),
        host_user=host.get("user", "inspire"),
        host_key=str(data_dir / host["key"]) if host.get("key") and not str(host["key"]).startswith("/")
        else host.get("key", ""),
        sites=list(host.get("sites") or []),
        models=models,
        default_model=default,
        fallback=fallback,
        memory_chars=mem.get("memory_chars", 2200),
        user_chars=mem.get("user_chars", 1400),
        max_context_tokens=ctx.get("max_tokens", 48000),
        compress_at=ctx.get("compress_at", 0.75),
        keep_last=ctx.get("keep_last", 12),
        idle_hours=ctx.get("idle_hours", 6),
        max_iterations=agent.get("max_iterations", 12),
        task_max_iterations=int(agent.get("task_max_iterations", 60)),
        task_max_minutes=int(agent.get("task_max_minutes", 30)),
        task_progress_seconds=int(agent.get("task_progress_seconds", 45)),
        request_timeout=agent.get("request_timeout", 75),
        chain_timeout=agent.get("chain_timeout", 150),
        timezone=raw.get("timezone", "Asia/Jakarta"),
        sandbox_url=shell.get("sandbox_url", ""),
        sandbox_token=shell.get("token", ""),
        shell_timeout=shell.get("timeout", 60),
        confirm_timeout=shell.get("confirm_timeout", 300),
        fetch_max_chars=web.get("fetch_max_chars", 12000),
        search_backends=web.get("search_backends") or ["duckduckgo", "bing", "yahoo"],
        media=MediaCfg(
            stt_api_base=stt.get("api_base", "https://api.groq.com/openai/v1"),
            stt_api_key=stt.get("api_key", ""),
            stt_model=stt.get("model", "whisper-large-v3-turbo"),
            vision_models=md.get("vision_models") or ["gemini/gemini-2.5-flash"],
            file_max_chars=md.get("file_max_chars", 20000),
            video_max_minutes=md.get("video_max_minutes", 60),
            ytdlp_cookies=md.get("ytdlp_cookies", ""),
        ),
        inbox_dir=Path(md["inbox_dir"]) if md.get("inbox_dir") else None,
    )
