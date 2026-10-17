"""Klasifikasi error panggilan model: dipakai wizard (petunjuk) dan LLM (cooldown / blokir)."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ModelProblem:
    field: str   # provider | context | key | limit | tools | model | server | url | other
    hint: str


def explain_model_error(msg: str, preset_id: str = "") -> ModelProblem:
    """Terjemahkan error mentah litellm jadi petunjuk + bagian yang harus diperbaiki."""
    m = msg.lower()
    if "free tier can only be use" in m or "only be used from within" in m:
        return ModelProblem("provider",
            "Penyedia ini melarang tier gratisnya dipakai dari aplikasi lain. Pilih penyedia lain "
            "(OpenRouter / Gemini / DeepSeek), atau model berbayar milik penyedia itu.")
    if any(x in m for x in ("context length", "context_length", "maximum context", "context window",
                            "too many tokens", "prompt is too long", "reduce the length", "input is too long")):
        return ModelProblem("context", "Percakapan melewati batas konteks model ini. Ringkas dengan /new, atau pakai model berkonteks lebih besar.")
    if any(x in m for x in ("401", "403", "unauthorized", "authentication", "invalid api key", "incorrect api key",
                            "invalid x-api-key", "api_key client option", "api key not valid", "permission denied",
                            "wrong api key", "invalid_api_key", "api key is invalid", "invalid key", "missing api key",
                            "no api key", "api key was not provided", "not authorized", "is not authorized")):
        return ModelProblem("key", "Key ditolak: salah, kedaluwarsa, atau belum diisi. Tempel ulang key-nya.")
    if any(x in m for x in ("429", "rate limit", "rate_limit", "quota", "resource_exhausted", "insufficient",
                            "too many requests", "retry in", "retry after")):
        return ModelProblem("limit",
            "Key valid, tapi kena limit / saldo habis. Coba lagi nanti, atau pilih model lain.")
    if "tool" in m and ("not support" in m or "unsupported" in m or "does not support" in m):
        return ModelProblem("tools", "Model ini tidak mendukung tool calling, padahal agent membutuhkannya. Pilih model lain.")
    if any(x in m for x in ("is unavailable", "model_not_found", "does not exist", "not found", "404",
                            "unknown model", "invalid model", "unsupported model", "llm provider not provided")):
        extra = " Cek daftar nama model di <URL>/models." if preset_id == "custom" else ""
        return ModelProblem("model",
            "Servernya menjawab, tapi nama model itu tidak ada, jadi URL dan key kemungkinan benar. "
            "Periksa ejaan nama model." + extra)
    if any(x in m for x in ("overloaded", "503", "502", "500", "internal server error", "service unavailable",
                            "bad gateway", "temporarily", "capacity")):
        return ModelProblem("server", "Server penyedia sedang bermasalah atau penuh. Biasanya pulih sendiri; dicoba lagi nanti.")
    if any(x in m for x in ("connect", "getaddrinfo", "name or service", "timed out", "timeout", "ssl",
                            "unreachable", "connection error", "refused")):
        return ModelProblem("url", "Tak bisa menghubungi server (atau terlalu lama menjawab). Periksa URL dan koneksi.")
    return ModelProblem("other", "Penyebabnya tak dikenali. Lihat detail di bawah, atau pilih penyedia lain.")


_RETRY = [
    re.compile(r"retry[- ]after\D{0,12}(\d+(?:\.\d+)?)", re.I),
    re.compile(r"retry in\s+(\d+(?:\.\d+)?)\s*s", re.I),
    re.compile(r"try again in\s+(\d+(?:\.\d+)?)\s*s", re.I),
]


def retry_after(msg: str) -> float | None:
    """Detik tunggu yang disarankan server (header / pesan), kalau ada."""
    for rx in _RETRY:
        if m := rx.search(msg):
            return float(m.group(1))
    return None
