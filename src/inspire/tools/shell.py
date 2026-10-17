"""Tool shell: jalan di container sandbox, perintah berisiko minta tombol ✅/❌."""

from __future__ import annotations

import re
import shlex
import time

import httpx

from . import Registry, Tool, ToolContext

# Perintah baca-saja yang boleh jalan tanpa tanya.
SAFE = {
    "ls", "cat", "head", "tail", "grep", "egrep", "wc", "pwd", "echo", "date",
    "whoami", "uname", "df", "du", "find", "sort", "uniq", "cut", "tr", "jq",
    "file", "stat", "tree", "which", "basename", "dirname", "realpath", "printf",
    "true", "cal", "uptime", "free", "nproc", "id", "md5sum", "sha256sum", "diff",
}
# Perintah coding umum: jalan otomatis selama semua path-nya di /workspace.
CODING = {
    "python", "python3", "pytest", "node", "npx", "npm", "pip", "pip3", "git",
    "mkdir", "touch", "cp", "mv", "zip", "unzip", "tar", "cd", "ruff", "black", "tsc",
}
# Sub-perintah yang tetap minta tombol.
CODING_RISKY = {("git", "push"), ("npm", "publish"), ("pip", "uninstall"), ("pip3", "uninstall")}
# find dengan opsi ini bisa mengubah / menjalankan sesuatu.
FIND_RISKY = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls"}
SPLIT = re.compile(r"\|\||&&|;|\||\n|&")


def _outside_workspace(words: list[str]) -> str | None:
    for w in words[1:]:
        val = w.split("=", 1)[1] if w.startswith("-") and "=" in w else w
        if val.startswith("/") and not (val == "/workspace" or val.startswith("/workspace/")
                                       or val == "/dev/null"):
            return val
    return None


def needs_confirm(command: str) -> str | None:
    """None = boleh jalan otomatis. Selain itu: alasan kenapa perlu persetujuan."""
    command = command.replace("2>&1", "")  # gabung stderr: bukan redirect ke file
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
        if cmd in CODING:
            if (cmd, words[1] if len(words) > 1 else "") in CODING_RISKY:
                return f"'{cmd} {words[1]}' keluar dari sandbox"
            if path := _outside_workspace(words):
                return f"menyentuh {path} di luar /workspace"
            continue
        if cmd not in SAFE:
            return f"'{cmd}' bukan perintah baca-saja / coding"
        if cmd == "find" and FIND_RISKY & set(words):
            return "find dengan aksi"
    return None


def is_readonly(command: str) -> bool:
    """Semua segmen hanya perintah baca (SAFE) → tak perlu masuk jurnal."""
    command = command.replace("2>&1", "")
    if any(t in command for t in (">", "`", "$(", "<(")):
        return False
    for seg in SPLIT.split(command):
        try:
            words = shlex.split(seg.strip())
        except ValueError:
            return False
        if words and (words[0] not in SAFE or (words[0] == "find" and FIND_RISKY & set(words))):
            return False
    return True


class Grants:
    """Izin sementara per chat: semua perintah sandbox jalan tanpa tombol."""

    def __init__(self, minutes: int = 30):
        self.minutes = minutes
        self._until: dict[str, float] = {}

    def grant(self, chat_id: str) -> float:
        self._until[chat_id] = time.time() + self.minutes * 60
        return self._until[chat_id]

    def active(self, chat_id: str) -> bool:
        return self._until.get(chat_id, 0) > time.time()

    def revoke(self, chat_id: str) -> bool:
        return self._until.pop(chat_id, 0) > time.time()


def register_shell(
    reg: Registry, url: str, token: str, timeout: int = 60, grants: Grants | None = None,
    journal=None,
) -> None:
    grants = grants or Grants()

    async def shell(ctx: ToolContext, command: str, timeout_s: int = timeout) -> str:
        reason = needs_confirm(command)
        # Izin 30 menit hanya untuk chat interaktif, tidak untuk jadwal.
        granted = not ctx.is_job and grants.active(ctx.chat_id)
        if reason and not ctx.auto_approve_shell and not granted:
            if not ctx.confirm:
                return f"Ditolak: perintah butuh persetujuan ({reason}) tapi tak bisa bertanya."
            text = f"🖥 Jalankan di sandbox? ({reason})\n\n$ {command[:3000]}"
            ok = await (ctx.confirm(text) if ctx.is_job else ctx.confirm(text, grant=True))
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
        if journal and not is_readonly(command):
            n = journal.add(ctx.chat_id, "sandbox", "shell", command[:300],
                            status="ok" if r["exit_code"] == 0 else f"exit {r['exit_code']}")
            out[0] += f" [jurnal #{n}, tak bisa dibatalkan otomatis]"
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
                "non-root, ada internet, python3, node, git, curl). File di /workspace awet. "
                "Perintah baca-saja dan coding umum (python, pytest, node, npm, pip, git, "
                "mkdir/cp/mv) di /workspace jalan langsung; yang lain menunggu persetujuan user. "
                "Untuk menulis file pakai write_file/edit_file, bukan redirect >."
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
