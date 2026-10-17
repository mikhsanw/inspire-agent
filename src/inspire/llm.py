"""Satu pintu ke semua model lewat litellm, dengan fallback berurutan."""

from __future__ import annotations

import logging

import litellm

from .config import ModelCfg

log = logging.getLogger(__name__)
litellm.drop_params = True       # buang parameter yang tak didukung provider
litellm.suppress_debug_info = True


class AllModelsFailed(RuntimeError):
    pass


class LLM:
    def __init__(
        self,
        models: dict[str, ModelCfg],
        default: str,
        fallback: list[str],
        timeout: float = 120,
    ):
        self.models = models
        self.active = default
        self.fallback = fallback
        self.timeout = timeout

    def set_active(self, name: str) -> bool:
        if name not in self.models:
            return False
        self.active = name
        return True

    def order(self) -> list[str]:
        first = [self.active] if self.active in self.models else []
        return first + [n for n in self.fallback if n != self.active and n in self.models]

    def update(self, models: dict[str, ModelCfg], default: str, fallback: list[str]) -> None:
        """Setelan berubah (mis. dari /setup) → pakai langsung, tanpa restart."""
        self.models, self.fallback = models, fallback
        if self.active not in models:
            self.active = default

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> tuple[dict, str]:
        """Kembalikan (pesan assistant format OpenAI, nama model yang menjawab)."""
        if not self.order():
            raise AllModelsFailed(
                "Belum ada model. Atur dengan /setup (Telegram) atau `inspire setup model`."
            )
        errors = []
        for name in self.order():
            cfg = self.models[name]
            try:
                resp = await litellm.acompletion(
                    model=cfg.model,
                    messages=messages if cfg.vision else strip_images(messages),
                    tools=tools or None,
                    api_base=cfg.api_base or None,
                    api_key=cfg.api_key or None,
                    timeout=self.timeout,
                    **cfg.extra,
                )
                return _to_dict(resp.choices[0].message), name
            except Exception as e:
                log.warning("model %s gagal: %s", name, e)
                errors.append(f"{name}: {type(e).__name__}: {str(e)[:200]}")
        raise AllModelsFailed("; ".join(errors))


def has_images(messages: list[dict]) -> bool:
    return any(isinstance(m.get("content"), list) for m in messages)


def strip_images(messages: list[dict]) -> list[dict]:
    """Model tanpa vision: ganti bagian gambar dengan catatan teks."""
    if not has_images(messages):
        return messages
    out = []
    for m in messages:
        if isinstance(m.get("content"), list):
            texts = [p["text"] for p in m["content"] if p.get("type") == "text"]
            n = sum(1 for p in m["content"] if p.get("type") == "image_url")
            texts.append(f"[{n} gambar dilampirkan, tapi model ini tidak bisa melihat gambar]")
            m = {**m, "content": "\n".join(texts)}
        out.append(m)
    return out


def _to_dict(message) -> dict:
    out: dict = {"role": "assistant", "content": message.content or ""}
    calls = getattr(message, "tool_calls", None) or []
    if calls:
        out["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {
                    "name": c.function.name,
                    "arguments": c.function.arguments or "{}",
                },
            }
            for c in calls
        ]
    return out
