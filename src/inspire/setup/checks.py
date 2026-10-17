"""Tes key/token sungguhan sebelum disimpan. Semua kembalikan (ok, pesan)."""

from __future__ import annotations

import httpx
import litellm

T = 20


async def _get(url: str, **kw) -> httpx.Response:
    async with httpx.AsyncClient(timeout=T) as c:
        return await c.get(url, **kw)


async def telegram(token: str) -> tuple[bool, str]:
    try:
        r = await _get(f"https://api.telegram.org/bot{token}/getMe")
    except httpx.HTTPError as e:
        return False, f"tak bisa menghubungi Telegram ({type(e).__name__})"
    if r.status_code != 200:
        return False, "token ditolak Telegram"
    return True, "@" + r.json()["result"]["username"]


async def discord(token: str) -> tuple[bool, str]:
    try:
        r = await _get("https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bot {token}"})
    except httpx.HTTPError as e:
        return False, f"tak bisa menghubungi Discord ({type(e).__name__})"
    if r.status_code != 200:
        return False, "token ditolak Discord"
    return True, r.json().get("username", "bot")


async def groq(key: str) -> tuple[bool, str]:
    try:
        r = await _get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as e:
        return False, f"tak bisa menghubungi Groq ({type(e).__name__})"
    return (True, "key Groq valid") if r.status_code == 200 else (False, f"key ditolak (HTTP {r.status_code})")


async def gemini(key: str) -> tuple[bool, str]:
    try:
        r = await _get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": key})
    except httpx.HTTPError as e:
        return False, f"tak bisa menghubungi Google ({type(e).__name__})"
    return (True, "key Gemini valid") if r.status_code == 200 else (False, f"key ditolak (HTTP {r.status_code})")


async def sandbox(url: str, token: str) -> tuple[bool, str]:
    if not url:
        return False, "sandbox_url kosong"
    try:
        r = await _get(f"{url.rstrip('/')}/health")
    except httpx.HTTPError as e:
        return False, f"sandbox tak menjawab ({type(e).__name__})"
    return (True, "sandbox hidup") if r.status_code == 200 else (False, f"HTTP {r.status_code}")


async def model(spec: dict) -> tuple[bool, str]:
    """Satu panggilan kecil sungguhan. spec = {model, api_base?, api_key?}."""
    try:
        resp = await litellm.acompletion(
            model=spec["model"],
            messages=[{"role": "user", "content": "Balas satu kata: OK"}],
            api_base=spec.get("api_base") or None,
            api_key=spec.get("api_key") or None,
            max_tokens=10,
            timeout=45,
        )
    except Exception as e:
        msg = str(e).split("\n")[0][:200]
        return False, f"{type(e).__name__}: {msg}"
    return True, f"model menjawab: {(resp.choices[0].message.content or '').strip()[:30]!r}"
