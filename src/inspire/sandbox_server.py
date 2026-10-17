"""Server eksekusi shell kecil, jalan DI DALAM container sandbox.

Tanpa dependensi (stdlib). Agent memanggilnya lewat HTTP di jaringan internal
compose, jadi sandbox tidak pernah melihat .env, database, atau memori agent.

    POST /exec  {"command": "...", "timeout": 60}
    Authorization: Bearer <SANDBOX_TOKEN>
"""

from __future__ import annotations

import hmac
import json
import os
import subprocess
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
        if self.path != "/exec":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            command = body["command"]
        except (ValueError, KeyError):
            return self._send(400, {"error": "butuh JSON {command}"})
        self._send(200, run(command, body.get("timeout", 60)))

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
