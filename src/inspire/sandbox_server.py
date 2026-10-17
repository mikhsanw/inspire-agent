"""Server eksekusi shell kecil, jalan DI DALAM container sandbox.

Tanpa dependensi (stdlib). Agent memanggilnya lewat HTTP di jaringan internal
compose, jadi sandbox tidak pernah melihat .env, database, atau memori agent.

    POST /exec      {"command": "...", "timeout": 60}
    POST /read      {"path", "offset"?, "limit"?}
    POST /write     {"path", "content"}
    POST /edit      {"path", "old", "new", "replace_all"?}
    POST /list      {"path"?, "depth"?}
    POST /download  {"path"}  → base64
    POST /delete    {"path"}  (dipakai rollback jurnal)
    POST /job/start {"argv": [...], "cwd"?, "env"?, "timeout"?}  → {"id"}   proses latar belakang (tanpa shell)
    POST /job/read  {"id", "offset"?}  → {"data", "next", "running", "exit_code", "timed_out", "err"}
    POST /job/kill  {"id"}
    Authorization: Bearer <SANDBOX_TOKEN>

Semua path dikurung di WORKSPACE (realpath, jadi symlink keluar juga ditolak).
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import signal
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("SANDBOX_TOKEN", "")
WORKSPACE = os.environ.get("WORKSPACE", "/workspace")
MAX_TIMEOUT = 300
MAX_OUTPUT = 20000

# Env bersih untuk perintah: token tidak ikut bocor ke `env` / /proc.
CLEAN_ENV = {
    "PATH": "/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME": os.environ.get("HOME", "/home/agent"),
    "LANG": "C.UTF-8",
    "TERM": "dumb",
}


def run(command: str, timeout: int) -> dict:
    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    try:
        p = subprocess.run(
            ["bash", "-lc", command],
            cwd=WORKSPACE,
            env=CLEAN_ENV,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "exit_code": p.returncode,
            "stdout": p.stdout[-MAX_OUTPUT:],
            "stderr": p.stderr[-MAX_OUTPUT:],
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as e:
        partial = e.stdout or ""
        if isinstance(partial, bytes):  # TimeoutExpired kadang bawa bytes walau text=True
            partial = partial.decode(errors="replace")
        return {
            "exit_code": -1,
            "stdout": partial[-MAX_OUTPUT:],
            "stderr": f"timeout setelah {timeout} detik",
            "timed_out": True,
        }


MAX_FILE = 20 * 1024 * 1024
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}


class FileError(Exception):
    pass


def resolve(path: str) -> str:
    root = os.path.realpath(WORKSPACE)
    p = path or "."
    if p.startswith("/workspace"):  # path sandbox ditulis absolut oleh model
        p = p[len("/workspace"):].lstrip("/") or "."
    real = os.path.realpath(p if os.path.isabs(p) else os.path.join(root, p))
    if real != root and not real.startswith(root + os.sep):
        raise FileError(f"'{path}' di luar /workspace")
    return real


def rel(real: str) -> str:
    r = os.path.relpath(real, os.path.realpath(WORKSPACE))
    return "/workspace" if r == "." else f"/workspace/{r}"


def _text(real: str) -> str:
    if not os.path.isfile(real):
        raise FileError(f"{rel(real)} tidak ada")
    if os.path.getsize(real) > MAX_FILE:
        raise FileError("file terlalu besar")
    data = open(real, "rb").read()
    if b"\0" in data[:8192]:
        raise FileError("file biner, tak bisa dibaca sebagai teks")
    return data.decode("utf-8", errors="replace")


def f_read(b: dict) -> dict:
    real = resolve(b["path"])
    lines = _text(real).splitlines()
    offset = max(1, int(b.get("offset", 1)))
    limit = max(1, min(int(b.get("limit", 400)), 2000))
    return {"path": rel(real), "total": len(lines), "offset": offset,
            "lines": lines[offset - 1: offset - 1 + limit]}


def f_write(b: dict) -> dict:
    real = resolve(b["path"])
    created = not os.path.exists(real)
    os.makedirs(os.path.dirname(real), exist_ok=True)
    with open(real, "w", encoding="utf-8") as f:
        f.write(b["content"])
    return {"path": rel(real), "created": created, "bytes": len(b["content"].encode())}


def f_edit(b: dict) -> dict:
    real = resolve(b["path"])
    text, old, new = _text(real), b["old"], b["new"]
    n = text.count(old) if old else 0
    if n == 0:
        raise FileError("teks 'old' tidak ditemukan persis. Baca ulang file dengan read_file.")
    if n > 1 and not b.get("replace_all"):
        raise FileError(f"teks 'old' muncul {n}x. Perpanjang supaya unik, atau replace_all=true.")
    text = text.replace(old, new) if b.get("replace_all") else text.replace(old, new, 1)
    with open(real, "w", encoding="utf-8") as f:
        f.write(text)
    return {"path": rel(real), "replaced": n if b.get("replace_all") else 1}


def f_list(b: dict) -> dict:
    root = resolve(b.get("path", "."))
    if not os.path.isdir(root):
        raise FileError(f"{rel(root)} bukan folder")
    depth = max(1, min(int(b.get("depth", 2)), 5))
    out, base = [], root.rstrip(os.sep).count(os.sep)
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        level = dirpath.count(os.sep) - base
        if level >= depth:
            dirs[:] = []
        indent = "  " * level
        if dirpath != root:
            out.append(f"{indent[:-2]}{os.path.basename(dirpath)}/")
        for name in sorted(files):
            size = os.path.getsize(os.path.join(dirpath, name))
            out.append(f"{indent}{name} ({size} B)")
        if len(out) > 400:
            out.append("… (dipotong)")
            break
    return {"path": rel(root), "entries": out}


def f_download(b: dict) -> dict:
    real = resolve(b["path"])
    if not os.path.isfile(real):
        raise FileError(f"{rel(real)} tidak ada")
    if os.path.getsize(real) > MAX_FILE:
        raise FileError("file > 20MB")
    return {"path": rel(real), "name": os.path.basename(real),
            "data": base64.b64encode(open(real, "rb").read()).decode()}


def f_delete(b: dict) -> dict:
    real = resolve(b["path"])
    if not os.path.isfile(real):
        raise FileError(f"{rel(real)} bukan file")
    os.remove(real)
    return {"path": rel(real), "deleted": True}


# --- job latar belakang -------------------------------------------------------------------------
# Untuk pekerjaan panjang (mis. OpenCode, bisa puluhan menit) yang tak muat di /exec sinkron.
# Proses jalan tanpa shell (argv langsung), stdin ditutup, di sesi sendiri supaya seluruh
# grup proses bisa dihentikan. Keluaran ditulis ke berkas dan dibaca bertahap lewat offset.

JOBS_DIR = os.environ.get("JOBS_DIR", os.path.join(tempfile.gettempdir(), "inspire-jobs"))
JOB_MAX_TIMEOUT = 4 * 3600
JOB_READ_CHUNK = 256 * 1024
MAX_JOBS_RUNNING = 3
JOBS: dict[str, "Job"] = {}
JOBS_LOCK = threading.Lock()


def descendants(pid: int) -> list[int]:
    """Semua keturunan proses (anak, cucu, …) lewat `ps`. Dipakai karena program seperti OpenCode
    menjalankan perintahnya di GRUP PROSES SENDIRI, sehingga killpg saja tak menjangkaunya."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    kids: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, stack = [], [pid]
    while stack:
        for child in kids.get(stack.pop(), []):
            found.append(child)
            stack.append(child)
    return found


class Job:
    def __init__(self, jid: str, proc: subprocess.Popen, out_path: str, err_path: str, timeout: float):
        self.id, self.proc, self.out_path, self.err_path = jid, proc, out_path, err_path
        self.timeout, self.started = timeout, time.time()
        self.exit_code: int | None = None
        self.timed_out = False
        self.killed = False
        self.known: set[int] = set()     # pid yang pernah terlihat; tetap dikejar walau induknya sudah mati

    def stop(self, sig: int = signal.SIGTERM) -> None:
        """Hentikan proses utama DAN seluruh keturunannya (termasuk yang lepas dari grup proses)."""
        self.known |= {self.proc.pid, *descendants(self.proc.pid)}   # ambil pohon SEBELUM induknya mati
        try:
            os.killpg(os.getpgid(self.proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            pass
        for pid in self.known:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass


def _watch(job: Job) -> None:
    try:
        job.exit_code = job.proc.wait(timeout=job.timeout)
    except subprocess.TimeoutExpired:
        job.timed_out = True
        job.stop(signal.SIGTERM)
        try:
            job.exit_code = job.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            job.stop(signal.SIGKILL)
            job.exit_code = job.proc.wait()


def _gc_jobs() -> None:
    """Buang job lama yang sudah selesai (>1 jam) beserta berkasnya."""
    now = time.time()
    with JOBS_LOCK:
        for jid in [j for j, job in JOBS.items() if job.exit_code is not None and now - job.started > 3600]:
            job = JOBS.pop(jid)
            for path in (job.out_path, job.err_path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def j_start(b: dict) -> dict:
    argv = b.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise FileError("argv harus list string tak kosong")
    cwd = resolve(b.get("cwd", "."))
    if not os.path.isdir(cwd):
        raise FileError(f"cwd {rel(cwd)} bukan folder")
    extra = b.get("env") or {}
    if not isinstance(extra, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in extra.items()):
        raise FileError("env harus objek string→string")
    timeout = max(1.0, min(float(b.get("timeout", 1800)), JOB_MAX_TIMEOUT))
    _gc_jobs()
    with JOBS_LOCK:
        if sum(1 for j in JOBS.values() if j.exit_code is None) >= MAX_JOBS_RUNNING:
            raise FileError(f"sudah ada {MAX_JOBS_RUNNING} job berjalan")
    os.makedirs(JOBS_DIR, exist_ok=True)
    jid = secrets.token_hex(6)
    out_path, err_path = os.path.join(JOBS_DIR, f"{jid}.out"), os.path.join(JOBS_DIR, f"{jid}.err")
    env = {**CLEAN_ENV, **extra}
    try:
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                    start_new_session=True)
    except OSError as e:
        raise FileError(f"gagal menjalankan {argv[0]}: {e.strerror or e}")
    job = Job(jid, proc, out_path, err_path, timeout)
    with JOBS_LOCK:
        JOBS[jid] = job
    threading.Thread(target=_watch, args=(job,), daemon=True).start()
    return {"id": jid}


def _job(b: dict) -> Job:
    job = JOBS.get(str(b.get("id", "")))
    if not job:
        raise FileError("job tak ada (sudah dibersihkan?)")
    return job


def j_read(b: dict) -> dict:
    job = _job(b)
    offset = max(0, int(b.get("offset", 0)))
    done = job.exit_code is not None                 # baca status SEBELUM berkas: tak ada baris tertinggal
    with open(job.out_path, "rb") as f:
        f.seek(offset)
        chunk = f.read(JOB_READ_CHUNK)
    if not done:                                     # selama jalan, hanya baris utuh
        cut = chunk.rfind(b"\n") + 1
        chunk = chunk[:cut]
    res = {"data": chunk.decode("utf-8", errors="replace"), "next": offset + len(chunk),
           "running": not done, "exit_code": job.exit_code, "timed_out": job.timed_out, "err": ""}
    if done:
        try:
            with open(job.err_path, "rb") as f:
                res["err"] = f.read()[-2000:].decode("utf-8", errors="replace")
        except OSError:
            pass
    return res


def j_kill(b: dict) -> dict:
    job = _job(b)
    job.killed = True
    job.stop(signal.SIGTERM)
    threading.Timer(3, lambda: job.exit_code is None and job.stop(signal.SIGKILL)).start()
    return {"killed": True}


JOB_OPS = {"/job/start": j_start, "/job/read": j_read, "/job/kill": j_kill}
FILE_OPS = {"/delete": f_delete, "/read": f_read, "/write": f_write, "/edit": f_edit, "/list": f_list, "/download": f_download}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._send(200, {"ok": True}) if self.path == "/health" else self._send(404, {})

    def do_POST(self) -> None:
        auth = self.headers.get("Authorization", "")
        if not TOKEN or not hmac.compare_digest(auth, f"Bearer {TOKEN}"):
            return self._send(401, {"error": "unauthorized"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except ValueError:
            return self._send(400, {"error": "body bukan JSON"})
        if self.path == "/exec":
            if "command" not in body:
                return self._send(400, {"error": "butuh JSON {command}"})
            return self._send(200, run(body["command"], body.get("timeout", 60)))
        op = FILE_OPS.get(self.path) or JOB_OPS.get(self.path)
        if not op:
            return self._send(404, {"error": "not found"})
        try:
            self._send(200, op(body))
        except FileError as e:
            self._send(400, {"error": str(e)})
        except KeyError as e:
            self._send(400, {"error": f"butuh field {e}"})
        except (OSError, ValueError) as e:
            self._send(400, {"error": f"{type(e).__name__}: {getattr(e, 'strerror', None) or e}"})

    def log_message(self, fmt, *args) -> None:
        print(f"[sandbox] {self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    if not TOKEN:
        raise SystemExit("SANDBOX_TOKEN wajib diisi")
    os.makedirs(WORKSPACE, exist_ok=True)
    port = int(os.environ.get("PORT", "8765"))
    print(f"[sandbox] listen :{port}, workspace {WORKSPACE}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
