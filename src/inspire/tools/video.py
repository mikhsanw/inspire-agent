"""Tool video_transcript: ambil transkrip dari link video (YouTube, dll) via yt-dlp."""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from ..media import Media, MediaError
from . import Registry, Tool, ToolContext
from .web import DATA_NOTE, _blocked_host


def register_video(reg: Registry, media: Media) -> None:
    async def video_transcript(ctx: ToolContext, url: str) -> str:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return "Error: hanya URL http/https."
        if err := await asyncio.to_thread(_blocked_host, p.hostname):
            return f"Error: {err}."
        try:
            text = await media.video_transcript(url)
        except MediaError as e:
            return f"Error: {e}"
        return f"{DATA_NOTE}\n{text}"

    reg.add(
        Tool(
            name="video_transcript",
            description=(
                "Ambil judul, deskripsi, dan transkrip dari link video (YouTube, TikTok, "
                "X, dll). Pakai subtitle kalau ada; kalau tidak, audio ditranskrip Whisper. "
                "Pakai saat user kirim link video dan minta ringkasan/isi."
            ),
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            fn=video_transcript,
        )
    )
