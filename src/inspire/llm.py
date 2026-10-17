"""Satu pintu ke semua model lewat litellm, dengan fallback yang tahan banting.

Model yang gagal tidak dicoba terus-menerus:
- kena limit / server bermasalah → "istirahat" (cooldown) dan dilewati sementara;
- key / nama model / diblokir penyedia / tak dukung tool → ditandai "perlu dibereskan"
  (dilewati 30 menit, atau sampai setelannya diubah);
- tiap model punya batas waktu sendiri, dan seluruh rantai punya batas total, jadi tiga model
  yang macet tak membuat bot diam berminggu-menit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import litellm

from .config import ModelCfg
from .llm_errors import ModelProblem, explain_model_error, retry_after

log = logging.getLogger(__name__)
litellm.drop_params = True       # buang parameter yang tak didukung provider
litellm.suppress_debug_info = True

BLOCKED = {"key", "model", "provider", "tools"}          # butuh perbaikan manusia
BLOCK_SECONDS = 1800
MIN_ATTEMPT = 8                                            # sisa waktu rantai minimal untuk mencoba lagi
SHORT = {
    "key": "key ditolak", "model": "nama model tak ada", "provider": "diblokir penyedia",
    "tools": "tak mendukung tool", "limit": "kena limit", "server": "server penyedia bermasalah",
    "url": "tak terjangkau / terlalu lambat", "context": "melewati batas konteks",
}


class AllModelsFailed(RuntimeError):
    def __init__(self, message: str, errors: list[tuple[str, str, str]] | None = None):
        super().__init__(message)
        self.errors = errors or []   # [(model, jenis, ringkasan)]


@dataclass
class Health:
    kind: str = ""
    reason: str = ""
    until: float = 0.0
    fails: int = 0
    blocked: bool = False


class LLM:
    def __init__(
        self,
        models: dict[str, ModelCfg],
        default: str,
        fallback: list[str],
        timeout: float = 75,
        chain_timeout: float = 150,
        clock=time.time,
    ):
        self.models = models
        self.active = default
        self.fallback = fallback
        self.timeout = timeout
        self.chain_timeout = chain_timeout
        self.clock = clock
        self.slack = 3.0                 # sabuk pengaman di atas timeout litellm (detik)
        self.health: dict[str, Health] = {}
        self.last_used = ""

    def set_active(self, name: str) -> bool:
        if name not in self.models:
            return False
        self.active = name
        return True

    def order(self) -> list[str]:
        first = [self.active] if self.active in self.models else []
        return first + [n for n in self.fallback if n != self.active and n in self.models]

    def update(self, models: dict[str, ModelCfg], default: str, fallback: list[str]) -> None:
        """Setelan berubah (mis. dari /setup) → pakai langsung, tanpa restart. Model yang
        setelannya berubah (mis. key baru) atau hilang dilupakan riwayat errornya."""
        for name in list(self.health):
            if name not in models or models[name] != self.models.get(name):
                del self.health[name]
        self.models, self.fallback = models, fallback
        if self.active not in models:
            self.active = default

    # --- kesehatan model ------------------------------------------------------------

    def status(self, name: str) -> tuple[str, str]:
        """('ok'|'cooling'|'blocked', rincian)."""
        h = self.health.get(name)
        if not h or h.until <= self.clock():
            return "ok", ""
        eta = int(h.until - self.clock())
        if h.blocked:
            return "blocked", f"{SHORT.get(h.kind, h.kind)}"
        return "cooling", f"istirahat {eta} dtk ({SHORT.get(h.kind, h.kind)})"

    def reset(self, name: str | None = None) -> None:
        if name:
            self.health.pop(name, None)
        else:
            self.health.clear()

    def _fail(self, name: str, prob: ModelProblem, msg: str) -> str:
        """Catat kegagalan → ringkasan untuk pengguna."""
        if prob.field == "context":      # bukan salah modelnya
            return SHORT["context"]
        h = self.health.setdefault(name, Health())
        h.fails += 1
        h.kind, h.reason = prob.field, " ".join(msg.split())[:160]
        if prob.field in BLOCKED:
            h.blocked, wait = True, BLOCK_SECONDS
        elif prob.field == "limit":
            hinted = retry_after(msg)
            wait = min(max(hinted, 5), 900) if hinted else min(60 * 2 ** (h.fails - 1), 900)
        elif prob.field in ("server", "url"):
            wait = min(30 * 2 ** (h.fails - 1), 300)
        else:
            wait = 20
        h.until = self.clock() + wait
        short = SHORT.get(prob.field) or h.reason[:80]
        return f"{short}, istirahat {int(wait)} dtk" if prob.field in ("limit", "server", "url") else short

    # --- panggilan ----------------------------------------------------------------------

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> tuple[dict, str]:
        """Kembalikan (pesan assistant format OpenAI, nama model yang menjawab)."""
        order = self.order()
        if not order:
            raise AllModelsFailed(
                "Belum ada model. Atur dengan /setup (Telegram) atau `inspire setup model`."
            )
        ready = [n for n in order if self.status(n)[0] == "ok"]
        resting = [n for n in order if n not in ready]
        # Semua sedang istirahat: coba yang paling cepat pulih, daripada langsung menyerah.
        candidates = ready or [min(resting, key=lambda n: self.health[n].until)]
        deadline = self.clock() + self.chain_timeout
        errors: list[tuple[str, str, str]] = []

        for name in candidates:
            remaining = deadline - self.clock()
            if errors and remaining < MIN_ATTEMPT:
                break                                    # rantai kehabisan waktu
            cfg = self.models[name]
            timeout = cfg.timeout or self.timeout
            if errors:
                timeout = max(5.0, min(timeout, remaining))
            try:
                resp = await asyncio.wait_for(
                    litellm.acompletion(
                        model=cfg.model,
                        messages=messages if cfg.vision else strip_images(messages),
                        tools=tools or None,
                        api_base=cfg.api_base or None,
                        api_key=cfg.api_key or None,
                        timeout=timeout,
                        **cfg.extra,
                    ),
                    timeout + self.slack,                # sabuk pengaman: koneksi menggantung
                )
            except Exception as e:                       # CancelledError (/stop) sengaja lolos
                msg = f"timed out setelah {timeout:.0f} detik" if isinstance(e, asyncio.TimeoutError) else str(e)
                prob = explain_model_error(msg)
                summary = self._fail(name, prob, msg)
                log.warning("model %s gagal (%s): %s", name, prob.field, " ".join(msg.split())[:200])
                errors.append((name, prob.field, summary))
                continue
            self.health.pop(name, None)
            self.last_used = name
            return _to_dict(resp.choices[0].message), name

        lines = [f"• {n}: {summary}" for n, _, summary in errors]
        lines += [f"• {n}: {self.status(n)[1]}" for n in resting if n not in candidates]
        raise AllModelsFailed("\n".join(lines) or "tak ada model yang bisa dicoba", errors)


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
