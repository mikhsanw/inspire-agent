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
        return [self.active] + [n for n in self.fallback if n != self.active]

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> tuple[dict, str]:
        """Kembalikan (pesan assistant format OpenAI, nama model yang menjawab)."""
        errors = []
        for name in self.order():
            cfg = self.models[name]
            try:
                resp = await litellm.acompletion(
                    model=cfg.model,
                    messages=messages,
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
