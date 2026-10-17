"""Tool shell: jalan di container sandbox, perintah berisiko minta tombol ✅/❌."""

from __future__ import annotations

import re
import shlex

import httpx

from . import Registry, Tool, ToolContext

# Perintah baca-saja yang boleh jalan tanpa tanya.
SAFE = {
    "ls", "cat", "head", "tail", "grep", "egrep", "wc", "pwd", "echo", "date",
    "whoami", "uname", "df", "du", "find", "sort", "uniq", "cut", "tr", "jq",
    "file", "stat", "tree", "which", "basename", "dirname", "realpath", "printf",
    "true", "cal", "uptime", "free", "nproc", "id", "md5sum", "sha256sum", "diff",
}
# find dengan opsi ini bisa mengubah / menjalankan sesuatu.
FIND_RISKY = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls"}
SPLIT = re.compile(r"\|\||&&|;|\||\n|&")


def needs_confirm(command: str) -> str | None:
    """None = aman. Selain itu: alasan kenapa perlu persetujuan."""
    if any(t in command for t in (">", "`", "$(", "<(")):
        return "ada redirect / substitusi"
    for seg in SPLIT.split(command):
        seg = seg.strip()
        if not seg:
            continue
        try:
            words = shlex.split(seg)
        except ValueError:
            return "sintaks tak bisa diurai"
        # Lewati assignment awal: FOO=bar cmd
        while words and re.match(r"^\w+=", words[0]):
            words = words[1:]
        if not words:
            return "assignment variabel"
        cmd = words[0]
        if cmd not in SAFE:
            return f"'{cmd}' bukan perintah baca-saja"
        if cmd == "find" and FIND_RISKY & set(words):
            return "find dengan aksi"
    return None


def register_shell(reg: Registry, url: str, token: str, timeout: int = 60) -> None:
    async def shell(ctx: ToolContext, command: str, timeout_s: int = timeout) -> str:
        reason = needs_confirm(command)
        if reason and not ctx.auto_approve_shell:
            if not ctx.confirm:
                return f"Ditolak: perintah butuh persetujuan ({reason}) tapi tak bisa bertanya."
            ok = await ctx.confirm(f"🖥 Jalankan di sandbox? ({reason})\n\n$ {command[:3000]}")
            if not ok:
                return "User menolak / tidak menjawab. Perintah TIDAK dijalankan."
        try:
            async with httpx.AsyncClient(timeout=timeout_s + 15) as client:
                resp = await client.post(
                    f"{url.rstrip('/')}/exec",
                    json={"command": command, "timeout": timeout_s},
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as e:
            return f"Error: sandbox tak bisa dihubungi ({type(e).__name__}: {e})."
        if resp.status_code != 200:
            return f"Error: sandbox HTTP {resp.status_code}: {resp.text[:200]}"
        r = resp.json()
        out = [f"exit_code: {r['exit_code']}" + (" (timeout)" if r.get("timed_out") else "")]
        if r.get("stdout"):
            out.append(f"stdout:\n{r['stdout'][-8000:]}")
        if r.get("stderr"):
            out.append(f"stderr:\n{r['stderr'][-4000:]}")
        return "\n".join(out)

    reg.add(
        Tool(
            name="shell",
            description=(
                "Jalankan perintah bash di sandbox Linux terpisah (cwd /workspace, user "
                "non-root, ada internet, python3, git, curl). File di /workspace awet. "
                "Perintah baca-saja jalan langsung; yang lain menunggu persetujuan user."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_s": {"type": "integer", "default": timeout, "description": "Maks 300."},
                },
                "required": ["command"],
            },
            fn=shell,
        )
    )
