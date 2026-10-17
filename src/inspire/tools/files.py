"""Tool file di sandbox (/workspace) + kirim file ke user.

Tulis/ubah file di sandbox tidak minta tombol: isinya terkurung di container,
tak bisa menyentuh server, rahasia, atau data agent.
"""

from __future__ import annotations

import base64
from typing import Awaitable, Callable

import httpx

from . import Registry, Tool, ToolContext

SendFile = Callable[[str, str, bytes, str], Awaitable[None]]  # chat_id, nama, data, caption


class Outbox:
    """Jalur kirim file ke chat. `send` diisi saat gateway sudah jalan."""

    def __init__(self) -> None:
        self.send: SendFile | None = None


class Sandbox:
    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token

    async def call(self, op: str, **body) -> dict:
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(f"{self.url}/{op}", json=body,
                                 headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError as e:
            raise RuntimeError(f"sandbox tak bisa dihubungi ({type(e).__name__})") from e
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            raise RuntimeError(data.get("error") or f"HTTP {r.status_code}")
        return data


def register_files(reg: Registry, sb: Sandbox, outbox: Outbox) -> None:
    async def read_file(ctx: ToolContext, path: str, offset: int = 1, limit: int = 400) -> str:
        try:
            r = await sb.call("read", path=path, offset=offset, limit=limit)
        except RuntimeError as e:
            return f"Error: {e}"
        width = len(str(r["offset"] + len(r["lines"])))
        body = "\n".join(f"{i:>{width}}│ {line}" for i, line in enumerate(r["lines"], r["offset"]))
        end = r["offset"] + len(r["lines"]) - 1
        more = f"\n… ({r['total'] - end} baris lagi, pakai offset={end + 1})" if end < r["total"] else ""
        return f"{r['path']} (baris {r['offset']}–{end} dari {r['total']})\n{body}{more}"

    async def write_file(ctx: ToolContext, path: str, content: str) -> str:
        try:
            r = await sb.call("write", path=path, content=content)
        except RuntimeError as e:
            return f"Error: {e}"
        return f"{'Dibuat' if r['created'] else 'Ditimpa'}: {r['path']} ({r['bytes']} byte)"

    async def edit_file(ctx: ToolContext, path: str, old: str, new: str, replace_all: bool = False) -> str:
        try:
            r = await sb.call("edit", path=path, old=old, new=new, replace_all=replace_all)
        except RuntimeError as e:
            return f"Error: {e}"
        return f"Diubah: {r['path']} ({r['replaced']} tempat)"

    async def list_files(ctx: ToolContext, path: str = "/workspace", depth: int = 2) -> str:
        try:
            r = await sb.call("list", path=path, depth=depth)
        except RuntimeError as e:
            return f"Error: {e}"
        return f"{r['path']}/\n" + ("\n".join(r["entries"]) or "(kosong)")

    async def send_file(ctx: ToolContext, path: str, caption: str = "") -> str:
        if not outbox.send:
            return "Error: pengiriman file belum siap."
        try:
            r = await sb.call("download", path=path)
        except RuntimeError as e:
            return f"Error: {e}"
        await outbox.send(ctx.chat_id, r["name"], base64.b64decode(r["data"]), caption)
        return f"Terkirim ke user: {r['name']}"

    p = {"path": {"type": "string", "description": "Relatif ke /workspace, atau absolut /workspace/…"}}
    for name, desc, params, req, fn in [
        ("read_file", "Baca file teks di sandbox, dengan nomor baris. Pakai offset/limit untuk file panjang.",
         {**p, "offset": {"type": "integer", "default": 1}, "limit": {"type": "integer", "default": 400}},
         ["path"], read_file),
        ("write_file", "Buat atau timpa file di sandbox (folder induk dibuat otomatis). Untuk file baru "
         "atau menulis ulang total; untuk ubah sebagian pakai edit_file.",
         {**p, "content": {"type": "string"}}, ["path", "content"], write_file),
        ("edit_file", "Ganti potongan teks di file sandbox. `old` harus persis sama (termasuk spasi/indentasi) "
         "dan unik; sertakan beberapa baris konteks kalau perlu.",
         {**p, "old": {"type": "string"}, "new": {"type": "string"},
          "replace_all": {"type": "boolean", "default": False}}, ["path", "old", "new"], edit_file),
        ("list_files", "Lihat isi folder sandbox (pohon, lewati .git/node_modules).",
         {**p, "depth": {"type": "integer", "default": 2}}, [], list_files),
        ("send_file", "Kirim file dari sandbox ke user sebagai lampiran chat (maks 20MB).",
         {**p, "caption": {"type": "string"}}, ["path"], send_file),
    ]:
        reg.add(Tool(name, desc, {"type": "object", "properties": params, "required": req}, fn))
