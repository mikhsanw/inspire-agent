"""Muat config.yaml. Nilai ${VAR} diganti dari environment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

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
    vision: bool = False          # dipakai fase 3
    extra: dict = field(default_factory=dict)  # diteruskan ke litellm apa adanya


@dataclass
class Config:
    data_dir: Path
    telegram_token: str
    allowed_user_ids: list[int]
    models: dict[str, ModelCfg]
    default_model: str
    fallback: list[str]
    memory_chars: int = 2200
    user_chars: int = 1400
    max_context_tokens: int = 48000
    compress_at: float = 0.75
    keep_last: int = 12
    idle_hours: float = 6
    max_iterations: int = 12
    request_timeout: float = 120


def load(path: Path | str) -> Config:
    raw = _expand(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
    tg = raw.get("telegram", {})
    m = raw.get("models", {})
    mem = raw.get("memory", {})
    ctx = raw.get("context", {})
    agent = raw.get("agent", {})

    models = {name: ModelCfg(**spec) for name, spec in (m.get("list") or {}).items()}
    if not models:
        raise ValueError("config: models.list kosong")
    default = m.get("default") or next(iter(models))
    fallback = [n for n in (m.get("fallback") or []) if n in models]

    # Boleh list YAML [1, 2] atau string "1,2" (mis. dari ${ALLOWED_USER_IDS}).
    raw_ids = tg.get("allowed_user_ids") or []
    if not isinstance(raw_ids, list):
        raw_ids = str(raw_ids).replace(",", " ").split()
    ids = [int(x) for x in raw_ids if str(x).strip()]

    return Config(
        data_dir=Path(raw.get("data_dir", "data")),
        telegram_token=tg.get("token", ""),
        allowed_user_ids=ids,
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
        request_timeout=agent.get("request_timeout", 120),
    )
