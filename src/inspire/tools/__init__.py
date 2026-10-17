"""Registry tool: schema untuk model + pemanggil."""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


# confirm(teks, grant=False) → bool. grant=True: tampilkan juga tombol "izinkan 30 menit".
Confirm = Callable[..., Awaitable[bool]]


@dataclass
class ToolContext:
    """Info per-panggilan yang tool butuhkan (chat mana, sesi mana)."""

    chat_id: str
    session_id: int
    confirm: Confirm | None = None     # tanya user (tombol ✅/❌); None = tak bisa tanya
    auto_approve_shell: bool = False   # job yang ditandai "boleh tanpa tanya"
    is_job: bool = False


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any | Awaitable[Any]]

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Registry:
    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def schemas(self, exclude: set[str] = frozenset()) -> list[dict]:
        return [t.schema() for t in self.tools.values() if t.name not in exclude]

    async def call(self, name: str, raw_args: str | dict | None, ctx: ToolContext) -> str:
        tool = self.tools.get(name)
        if not tool:
            return f"Error: tool '{name}' tidak ada. Tool tersedia: {', '.join(self.tools)}"
        try:
            args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")
        except json.JSONDecodeError as e:
            # Model free sering kirim JSON rusak; kasih tahu biar dia coba lagi.
            return f"Error: argumen bukan JSON valid ({e}). Kirim ulang dengan JSON benar."
        try:
            # Cek argumen dulu, supaya TypeError dari DALAM tool tak disangka argumen salah.
            inspect.signature(tool.fn).bind(ctx, **args)
        except TypeError as e:
            return f"Error: argumen salah untuk '{name}': {e}"
        try:
            result = tool.fn(ctx, **args)
            if inspect.isawaitable(result):
                result = await result
        except Exception as e:  # tool gagal tidak boleh matikan loop
            log.exception("tool %s gagal", name)
            return f"Error: {type(e).__name__}: {e}"
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
