"""Menjalankan OpenCode (https://opencode.ai) di dalam sandbox sebagai mesin /opencode.

Hal-hal yang SUDAH DIAMATI dengan OpenCode 1.19 asli (bukan tebakan):
- `opencode run` menunggu stdin sampai EOF; kalau stdin tak ditutup ia menggantung. Job sandbox
  selalu menutup stdin.
- `--format json` menulis JSONL ke stdout: step_start, tool_use (selesai: tool, input, output,
  status), text, step_finish (token, biaya), dan error. Kode keluar 1 kalau error.
- Tanpa `--auto`, pertanyaan izin ditolak otomatis (tidak menggantung). Izin "bash" bawaannya
  allow; `external_directory: deny` hanya membatasi tool FILE-nya, bukan bash. Batas sesungguhnya
  adalah container sandbox. Daftar `deny` di bawah hanya pengaman kecil, bukan pembatas.
- `--continue` melanjutkan sesi terakhir GLOBAL, jadi sesi disimpan per tugas dan dilanjutkan dengan -s.
- `--pure` menghindari instalasi plugin.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

# Pengaman kecil terhadap salah ketik / aksi ceroboh. BUKAN pembatas keamanan: bash bisa diakali
# dengan banyak cara. Pembatasnya container sandbox + titik simpan git.
PERMISSION = {
    "external_directory": "deny",
    "doom_loop": "deny",
    "bash": {
        "*": "allow",
        "rm -rf *": "deny", "rm -fr *": "deny", "rm -r *": "deny", "rm -R *": "deny", "rm --recursive *": "deny",
        "sudo *": "deny", "su *": "deny", "git push *": "deny", "chmod -R *": "deny", "chown *": "deny",
        "dd *": "deny", "mkfs*": "deny", "shutdown*": "deny", "reboot*": "deny", "pkill *": "deny",
    },
}

FRAME = (
    "Kamu bekerja di folder proyek ini (direktori kerja saat ini). Kerjakan tugas di bawah sampai selesai: "
    "tulis kode, jalankan, dan tes hasilnya sebelum menyatakan selesai. Jangan menyentuh file di luar "
    "folder ini. Jangan bertanya kecuali benar-benar buntu; kalau ada asumsi penting, tulis di jawaban "
    "akhir. Jawaban akhir: ringkasan 3–6 baris dalam bahasa Indonesia (apa yang dibuat/diubah, cara "
    "menjalankan, yang belum beres).\n\nTUGAS:\n"
)


def build_prompt(task: str) -> str:
    return FRAME + task.strip()


def build_argv(binary: str, prompt: str, model: str = "", session: str = "") -> list[str]:
    argv = [binary, "run", "--pure", "--format", "json"]
    if model:
        argv += ["-m", model]
    if session:
        argv += ["-s", session]
    return argv + [prompt]


def build_env(key: str = "") -> dict[str, str]:
    env = {
        "OPENCODE_PERMISSION": json.dumps(PERMISSION),
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    }
    if key:
        env["OPENCODE_API_KEY"] = key   # hanya untuk job ini; shell sandbox biasa tak melihatnya
    return env


def short_path(path: str) -> str:
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    return "/".join(parts[-2:]) if len(parts) > 1 else path


def brief_tool(tool: str, state: dict) -> str:
    """Ringkasan satu langkah untuk kabar kemajuan."""
    inp = state.get("input") or {}
    mark = " ✗" if state.get("status") == "error" else ""
    if tool in ("write", "edit", "patch", "multiedit"):
        verb = "tulis" if tool == "write" else "ubah"
        return f"{verb} {short_path(str(inp.get('filePath') or inp.get('path') or '?'))}{mark}"
    if tool == "read":
        return f"baca {short_path(str(inp.get('filePath') or inp.get('path') or '?'))}{mark}"
    if tool == "bash":
        return " ".join(str(inp.get("command", "")).split())[:60] + mark
    if tool in ("grep", "glob", "list"):
        return f"cari {str(inp.get('pattern') or inp.get('path') or '')[:40]}{mark}"
    if tool == "webfetch":
        return f"ambil {str(inp.get('url', ''))[:50]}{mark}"
    return f"{tool}{mark}"


@dataclass
class Summary:
    """Akumulasi event JSONL satu run."""

    session: str = ""
    steps: int = 0                      # jumlah tool_use
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    error: dict | None = None           # {"name", "message", "status"}
    tools: dict[str, int] = field(default_factory=dict)
    failed_tools: int = 0
    _pending: list[str] = field(default_factory=list)   # teks sejak tool terakhir
    _all_text: list[str] = field(default_factory=list)

    @property
    def final_text(self) -> str:
        """Jawaban akhir = teks sesudah tool terakhir; kalau tak ada, teks terakhir yang pernah muncul."""
        return "\n".join(self._pending).strip() or "\n".join(self._all_text[-1:]).strip()

    def feed(self, line: str) -> str | None:
        """Proses satu baris. Mengembalikan ringkasan langkah kalau itu tool_use, selain itu None."""
        line = line.strip()
        if not line:
            return None
        try:
            ev = json.loads(line)
        except ValueError:
            return None
        if not isinstance(ev, dict):
            return None
        self.session = self.session or ev.get("sessionID") or ""
        part = ev.get("part") or {}
        kind = ev.get("type")
        if kind == "tool_use":
            self.steps += 1
            self._pending = []
            tool = str(part.get("tool") or "?")
            self.tools[tool] = self.tools.get(tool, 0) + 1
            state = part.get("state") or {}
            if state.get("status") == "error":
                self.failed_tools += 1
            return brief_tool(tool, state)
        if kind == "text":
            text = str(part.get("text") or "")
            if text.strip():
                self._pending.append(text)
                self._all_text.append(text)
        elif kind == "step_finish":
            tok = part.get("tokens") or {}
            self.tokens_in += int(tok.get("input") or 0)
            self.tokens_out += int(tok.get("output") or 0)
            self.cost += float(part.get("cost") or 0)
        elif kind == "error":
            err = ev.get("error") or {}
            data = err.get("data") or {}
            self.error = {"name": str(err.get("name") or "Error"), "message": str(data.get("message") or err),
                          "status": data.get("statusCode")}
        return None


@dataclass
class Outcome:
    summary: Summary
    exit_code: int | None
    timed_out: bool = False
    limited: bool = False               # dihentikan karena melewati batas langkah
    stderr: str = ""


async def run(
    sandbox,
    project: str,
    prompt: str,
    *,
    binary: str = "opencode",
    model: str = "",
    session: str = "",
    key: str = "",
    timeout: float = 1800,
    max_steps: int = 100,
    poll: float = 2.0,
    on_step: Callable[[str, int], Awaitable[None]] | None = None,
) -> Outcome:
    """Jalankan satu tugas OpenCode di folder proyek sandbox dan alirkan event-nya."""
    job = (await sandbox.call("job/start", argv=build_argv(binary, build_prompt(prompt), model, session),
                              cwd=project, env=build_env(key), timeout=timeout))["id"]
    summary, offset, limited, partial = Summary(), 0, False, ""
    try:
        while True:
            r = await sandbox.call("job/read", id=job, offset=offset)
            offset = r["next"]
            lines = (partial + r["data"]).split("\n")
            partial = lines.pop()                    # baris terakhir mungkin belum utuh
            if not r["running"] and partial:
                lines.append(partial)
                partial = ""
            for line in lines:
                brief = summary.feed(line)
                if brief is not None and on_step:
                    await on_step(brief, summary.steps)
            if not r["running"]:
                return Outcome(summary, r["exit_code"], r["timed_out"], limited, r["err"])
            if summary.steps > max_steps and not limited:
                limited = True
                await sandbox.call("job/kill", id=job)
            await asyncio.sleep(poll)
    except asyncio.CancelledError:
        # /stop atau batas waktu: pastikan proses di sandbox ikut berhenti.
        try:
            await asyncio.shield(sandbox.call("job/kill", id=job))
        except Exception:
            log.exception("gagal menghentikan job opencode")
        raise


async def capture(sandbox, argv: list[str], env: dict[str, str] | None = None, timeout: float = 60,
                  poll: float = 0.3) -> tuple[int | None, str, str]:
    """Jalankan perintah pendek lewat job sandbox → (kode keluar, stdout, stderr). Untuk `--version`, `models`."""
    job = (await sandbox.call("job/start", argv=argv, env=env or {}, timeout=timeout))["id"]
    out, offset = "", 0
    while True:
        r = await sandbox.call("job/read", id=job, offset=offset)
        out += r["data"]
        offset = r["next"]
        if not r["running"]:
            return r["exit_code"], out, r["err"]
        await asyncio.sleep(poll)


def explain_error(err: dict) -> str:
    """Pesan error OpenCode → petunjuk untuk pengguna."""
    from .llm_errors import explain_model_error

    msg = f"{err.get('status') or ''} {err.get('message', '')}".strip()
    prob = explain_model_error(msg)
    detail = " ".join(str(err.get("message", "")).split())[:200]
    if err.get("name") == "UnknownError" and prob.field == "other":
        return ("OpenCode gagal (kemungkinan nama model salah atau model tak tersedia). Lihat daftar: "
                f"/opencode models\nDetail: {detail}")
    return f"OpenCode gagal: {prob.hint}\nDetail: {detail}"


def parse_models(text: str) -> list[str]:
    """Keluaran `opencode models` → daftar provider/model."""
    return [ln.strip() for ln in text.splitlines() if "/" in ln.strip() and " " not in ln.strip()]


__all__ = ["Summary", "Outcome", "run", "capture", "build_argv", "build_env", "build_prompt", "brief_tool", "explain_error",
           "parse_models", "PERMISSION"]
