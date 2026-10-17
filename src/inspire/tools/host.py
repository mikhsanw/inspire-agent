"""Tool server web user (host) lewat SSH.

Aturan (keputusan desain):
- Baca langsung jalan. Ubah file = diff → tombol. Perintah yang mengubah = selalu
  tombol (izin 30 menit TIDAK berlaku di sini).
- Sebelum mengubah: snapshot git (refs/inspire/<waktu>, tidak mengusik working
  tree) atau salinan file kalau bukan repo git.
- File rahasia (.env*, *.pem, *.key, id_rsa*) tampil sebagai KEY=***; nilai
  rahasia dari .env situs disamarkan di SEMUA keluaran. Satu nilai bisa dibuka
  lewat host_reveal + tombol.
- Key SSH ada di container agent, tidak di sandbox.
"""

from __future__ import annotations

import asyncio
import difflib
import io
import logging
import posixpath
import re
import shlex
import time

from dotenv import dotenv_values

from . import Registry, Tool, ToolContext
from .shell import SPLIT

log = logging.getLogger(__name__)

SECRET_FILE = re.compile(r"(^|/)(\.env(\.[\w.-]+)?|[^/]*\.pem|[^/]*\.key|id_(rsa|ed25519|ecdsa)[^/]*)$")
SECRET_FILE_IN_CMD = re.compile(r"\.env\b|\.pem\b|\.key\b|id_(rsa|ed25519|ecdsa)")
SECRET_KEY = re.compile(r"PASS|SECRET|KEY|TOKEN|PRIVATE|SALT|DSN|AUTH|CREDENTIAL|_USERNAME$", re.I)
ENV_LINE = re.compile(r"^(\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_.]*\s*=)(.*)$")
PEM = re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.S)

# Perintah baca-saja di server.
HOST_SAFE = {
    "ls", "cat", "head", "tail", "grep", "egrep", "wc", "pwd", "echo", "date", "whoami",
    "uname", "df", "du", "find", "sort", "uniq", "cut", "tr", "jq", "file", "stat", "tree",
    "which", "realpath", "diff", "md5sum", "sha256sum", "free", "uptime", "ps", "id", "nproc",
}
HOST_SAFE_SUB = {
    "git": {"status", "log", "diff", "show", "branch", "rev-parse", "blame", "ls-files", "describe", "remote"},
    "composer": {"show", "diagnose", "validate", "outdated", "--version"},
    "npm": {"ls", "outdated", "--version", "-v"},
    "node": {"--version", "-v"},
    "systemctl": {"status", "is-active"},
}
ARTISAN_SAFE = {
    "--version", "about", "list", "route:list", "migrate:status", "schedule:list",
    "queue:failed", "env", "config:show", "event:list", "model:show", "db:show", "db:table",
}
CURL_WRITE_FLAGS = {"-X", "--request", "-d", "--data", "--data-raw", "--data-binary", "-F",
                    "--form", "-T", "--upload-file", "-o", "--output", "-O", "--remote-name"}
SERVICE = re.compile(r"^(nginx|apache2|php[\d.]*-fpm)$")


def host_needs_confirm(command: str) -> str | None:
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
        if not words:
            continue
        cmd, sub = words[0], (words[1] if len(words) > 1 else "")
        if cmd in HOST_SAFE:
            if cmd == "find" and {"-exec", "-execdir", "-delete", "-ok"} & set(words):
                return "find dengan aksi"
            continue
        if cmd in HOST_SAFE_SUB and sub in HOST_SAFE_SUB[cmd]:
            if cmd == "git" and sub == "remote" and words[2:] not in ([], ["-v"]):
                return "git remote mengubah"
            if cmd == "git" and sub == "branch" and any(w.startswith("-") and w not in ("-a", "-v", "-vv", "--list") for w in words[2:]):
                return "git branch mengubah"
            continue
        if cmd == "php" and sub == "artisan" and len(words) > 2 and words[2] in ARTISAN_SAFE:
            continue
        if cmd == "curl":
            # -o /dev/null (cek status HTTP) tidak menulis apa pun.
            risky = [w for i, w in enumerate(words) if w in CURL_WRITE_FLAGS
                     and not (w in ("-o", "--output") and words[i + 1:i + 2] == ["/dev/null"])]
            if not risky:
                continue
        return f"'{' '.join(words[:3])}' bisa mengubah server"
    return None


def mask_env(text: str) -> str:
    out = []
    for line in text.splitlines():
        m = ENV_LINE.match(line)
        out.append(f"{m.group(1)}***" if m and m.group(2).strip() else line)
    return "\n".join(out)


class Host:
    """Jalankan skrip bash di server lewat ssh (key di container agent)."""

    def __init__(self, address: str, port: int, user: str, key: str, known_hosts: str):
        self.address, self.port, self.user, self.key, self.known_hosts = address, port, user, key, known_hosts

    def as_owner(self, owner: str, script: str) -> str:
        return f"sudo -n -u {shlex.quote(owner)} -H bash -c {shlex.quote(script)}"

    def as_admin(self, script: str) -> str:
        return script  # user inspire sendiri; sudo terbatas diatur sudoers

    async def run(self, script: str, stdin: bytes | None = None, timeout: int = 120) -> tuple[int, str, str]:
        argv = [
            "ssh", "-i", self.key, "-p", str(self.port),
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "ConnectTimeout=10",
            f"{self.user}@{self.address}", script,
        ]
        return await _exec(argv, stdin, timeout)


async def _exec(argv: list[str], stdin: bytes | None, timeout: int,
                env: dict | None = None) -> tuple[int, str, str]:
    p = await asyncio.create_subprocess_exec(
        *argv, env=env, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(p.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return -1, "", f"timeout setelah {timeout} detik"
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


class Sites:
    """Akses per situs: path dikurung di root situs, rahasia disamarkan."""

    def __init__(self, host: Host, sites: list[dict]):
        self.host = host
        self.sites = {s["name"]: s for s in sites}
        self._secrets: dict[str, tuple[float, dict[str, str]]] = {}

    def get(self, name: str) -> dict:
        if name not in self.sites:
            raise ValueError(f"situs '{name}' tak terdaftar. Ada: {', '.join(self.sites) or '-'}")
        return self.sites[name]

    def path(self, site: dict, rel: str) -> str:
        root = site["path"].rstrip("/")
        p = rel if rel.startswith("/") else posixpath.join(root, rel or ".")
        p = posixpath.normpath(p)
        if p != root and not p.startswith(root + "/"):
            raise ValueError(f"'{rel}' di luar folder situs {root}")
        return p

    def _guard(self, site: dict, full: str) -> str:
        """Potongan bash: realpath di server juga harus di dalam root (symlink).
        GNU realpath -m; cadangan python3 untuk sistem tanpa -m."""
        root = shlex.quote(site["path"].rstrip("/"))
        return (
            "rp() { realpath -m -- \"$1\" 2>/dev/null || "
            "python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' \"$1\"; }; "
            f'r=$(rp {root}); p=$(rp {shlex.quote(full)}); '
            'case "$p" in "$r"|"$r"/*) ;; *) echo "di luar folder situs" >&2; exit 3;; esac; '
        )

    async def owner_run(self, site: dict, script: str, stdin: bytes | None = None,
                        timeout: int = 120) -> tuple[int, str, str]:
        return await self.host.run(self.host.as_owner(site["owner"], script), stdin, timeout)

    # --- rahasia ----------------------------------------------------------

    async def env(self, site: dict) -> dict[str, str]:
        ts, vals = self._secrets.get(site["name"], (0, {}))
        if time.time() - ts < 300:
            return vals
        root = shlex.quote(site["path"].rstrip("/"))
        code, out, _ = await self.owner_run(site, f"cat {root}/.env 2>/dev/null")
        vals = {k: v or "" for k, v in dotenv_values(stream=io.StringIO(out)).items()} if code == 0 else {}
        self._secrets[site["name"]] = (time.time(), vals)
        return vals

    async def redact(self, site: dict, text: str, secret_file: bool = False) -> str:
        if secret_file:
            text = mask_env(text)
        text = PEM.sub("-----BEGIN ***-----\n***\n-----END ***-----", text)
        env = await self.env(site)
        values = sorted({v for k, v in env.items() if SECRET_KEY.search(k) and len(v) >= 6}, key=len, reverse=True)
        for v in values:
            text = text.replace(v, "***")
        return text

    # --- snapshot -----------------------------------------------------------

    async def snapshot(self, site: dict, files: list[str] = ()) -> str:
        """git → refs/inspire/<ts>. Bukan git → salin file yang akan diubah."""
        root = shlex.quote(site["path"].rstrip("/"))
        ts = time.strftime("%Y%m%d-%H%M%S")
        copies = " ".join(
            f'[ -e {shlex.quote(f)} ] && mkdir -p "$b/$(dirname {shlex.quote(posixpath.relpath(f, site["path"]))})" '
            f'&& cp -p {shlex.quote(f)} "$b/{posixpath.relpath(f, site["path"])}";'
            for f in files
        )
        script = (
            f"cd {root} && if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then "
            f"s=$(git stash create 'inspire snapshot {ts}'); [ -z \"$s\" ] && s=$(git rev-parse HEAD); "
            f"git update-ref refs/inspire/{ts} \"$s\" && echo git:{ts}; "
            f"else b=$HOME/.inspire-backup/{shlex.quote(site['name'])}/{ts}; mkdir -p \"$b\"; {copies} echo file:{ts}; fi"
        )
        code, out, err = await self.owner_run(site, script)
        return out.strip().splitlines()[-1] if code == 0 and out.strip() else f"gagal ({err.strip()[:100]})"


def register_host(reg: Registry, sites: Sites) -> None:
    async def ask(ctx: ToolContext, text: str) -> bool:
        if not ctx.confirm:
            return False
        return await ctx.confirm(text)  # tanpa grant: izin 30 menit tak berlaku di server

    async def host_sites(ctx: ToolContext) -> str:
        if not sites.sites:
            return "Belum ada situs. User perlu menjalankan `inspire setup web` di server."
        return "\n".join(f"- {s['name']}: {s['path']} (owner {s['owner']})" for s in sites.sites.values())

    async def host_run(ctx: ToolContext, site: str, command: str, timeout: int = 120) -> str:
        try:
            st = sites.get(site)
        except ValueError as e:
            return f"Error: {e}"
        reason = host_needs_confirm(command)
        snap = ""
        if reason:
            if ctx.is_job:
                return f"Ditolak: perintah mengubah server ({reason}) tak boleh dari jadwal."
            if not await ask(ctx, f"⚠️ SERVER · {site}\n$ {command[:2500]}\n\n({reason}; snapshot dibuat dulu)"):
                return "User menolak / tidak menjawab. Perintah TIDAK dijalankan."
            snap = await sites.snapshot(st)
        root = shlex.quote(st["path"].rstrip("/"))
        code, out, err = await sites.owner_run(st, f"cd {root} && {command}", timeout=min(timeout, 600))
        secret = bool(SECRET_FILE_IN_CMD.search(command))
        out = await sites.redact(st, out[-10000:], secret)
        err = await sites.redact(st, err[-4000:], secret)
        res = [f"exit_code: {code}"]
        if snap:
            res.append(f"snapshot: {snap} (host_rollback untuk kembali)")
        if out.strip():
            res.append(f"stdout:\n{out}")
        if err.strip():
            res.append(f"stderr:\n{err}")
        return "\n".join(res)

    async def host_read(ctx: ToolContext, site: str, path: str, offset: int = 1, limit: int = 300) -> str:
        try:
            st = sites.get(site)
            full = sites.path(st, path)
        except ValueError as e:
            return f"Error: {e}"
        offset, limit = max(1, offset), max(1, min(limit, 1500))
        script = (sites._guard(st, full) +
                  f'[ -f "$p" ] || {{ echo "file tidak ada" >&2; exit 4; }}; '
                  f'echo "TOTAL $(wc -l < "$p")"; sed -n \'{offset},{offset + limit - 1}p\' "$p"')
        code, out, err = await sites.owner_run(st, script)
        if code != 0:
            return f"Error: {err.strip() or out.strip()}"
        head, _, body = out.partition("\n")
        total = head.removeprefix("TOTAL ").strip()
        body = await sites.redact(st, body, bool(SECRET_FILE.search(full)))
        lines = body.splitlines()
        num = "\n".join(f"{i:>5}│ {line}" for i, line in enumerate(lines, offset))
        return f"{full} (baris {offset}–{offset + len(lines) - 1} dari {total})\n{num}"

    async def _write(ctx: ToolContext, st: dict, full: str, new: str, old_text: str | None) -> str:
        diff = "".join(difflib.unified_diff(
            (old_text or "").splitlines(True), new.splitlines(True),
            fromfile=f"a/{posixpath.relpath(full, st['path'])}", tofile=f"b/{posixpath.relpath(full, st['path'])}",
            n=2,
        ))
        if not diff:
            return "Tak ada perubahan."
        shown = await sites.redact(st, diff)
        if len(shown) > 3300:
            shown = shown[:3300] + "\n… (diff dipotong)"
        if ctx.is_job:
            return "Ditolak: mengubah file server tak boleh dari jadwal."
        if not await ask(ctx, f"✏️ SERVER · {st['name']} · {'file baru' if old_text is None else 'ubah file'}\n\n{shown}"):
            return "User menolak / tidak menjawab. File TIDAK diubah."
        snap = await sites.snapshot(st, [full])
        script = sites._guard(st, full) + 'mkdir -p "$(dirname "$p")" && cat > "$p"'
        code, _, err = await sites.owner_run(st, script, stdin=new.encode())
        if code != 0:
            return f"Error menulis: {err.strip()}"
        return f"Tersimpan: {full}. snapshot: {snap} (host_rollback untuk kembali)"

    async def _current(st: dict, full: str) -> tuple[int, str, str]:
        script = sites._guard(st, full) + '[ -f "$p" ] || exit 4; cat -- "$p"'
        return await sites.owner_run(st, script)

    async def host_edit(ctx: ToolContext, site: str, path: str, old: str, new: str,
                        replace_all: bool = False) -> str:
        try:
            st = sites.get(site)
            full = sites.path(st, path)
        except ValueError as e:
            return f"Error: {e}"
        if SECRET_FILE.search(full):
            return "Ditolak: file rahasia tak boleh diubah agent. Minta user mengubahnya sendiri."
        code, text, err = await _current(st, full)
        if code != 0:
            return f"Error: {'file tidak ada' if code == 4 else err.strip()}"
        n = text.count(old) if old else 0
        if n == 0:
            return "Error: teks 'old' tidak ditemukan persis. Baca ulang dengan host_read."
        if n > 1 and not replace_all:
            return f"Error: teks 'old' muncul {n}x. Perpanjang supaya unik, atau replace_all=true."
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        return await _write(ctx, st, full, updated, text)

    async def host_write(ctx: ToolContext, site: str, path: str, content: str) -> str:
        try:
            st = sites.get(site)
            full = sites.path(st, path)
        except ValueError as e:
            return f"Error: {e}"
        if SECRET_FILE.search(full):
            return "Ditolak: file rahasia tak boleh diubah agent. Minta user mengubahnya sendiri."
        code, text, err = await _current(st, full)
        if code not in (0, 4):
            return f"Error: {err.strip()}"
        return await _write(ctx, st, full, content, text if code == 0 else None)

    async def host_reveal(ctx: ToolContext, site: str, key: str) -> str:
        try:
            st = sites.get(site)
        except ValueError as e:
            return f"Error: {e}"
        env = await sites.env(st)
        if key not in env:
            return f"Kunci '{key}' tak ada di .env {site}."
        if not await ask(ctx, f"🔑 Tampilkan nilai {key} dari .env situs {site} ke model AI?"):
            return "User menolak. Nilai tetap tersembunyi."
        return f"{key}={env[key]}"

    async def host_rollback(ctx: ToolContext, site: str, snapshot: str = "") -> str:
        try:
            st = sites.get(site)
        except ValueError as e:
            return f"Error: {e}"
        root = shlex.quote(st["path"].rstrip("/"))
        name = shlex.quote(st["name"])
        if not snapshot:
            script = (f"cd {root}; git for-each-ref --sort=-refname --count=10 --format='git:%(refname:short)' "
                      f"refs/inspire 2>/dev/null | sed 's#inspire/##'; ls -1r $HOME/.inspire-backup/{name} 2>/dev/null "
                      f"| head -10 | sed 's/^/file:/'")
            _, out, _ = await sites.owner_run(st, script)
            return "Snapshot (terbaru dulu):\n" + (out.strip() or "(belum ada)")
        kind, _, ts = snapshot.partition(":")
        if kind not in ("git", "file") or not re.fullmatch(r"\d{8}-\d{6}", ts):
            return "Error: format snapshot 'git:YYYYMMDD-HHMMSS' atau 'file:YYYYMMDD-HHMMSS'."
        if not await ask(ctx, f"⏪ SERVER · {site}\nKembalikan file ke snapshot {snapshot}?"):
            return "User menolak. Tidak ada yang dikembalikan."
        if kind == "git":
            script = f"cd {root} && git checkout refs/inspire/{ts} -- . && git status --short | head -30"
        else:
            script = f"cp -rp $HOME/.inspire-backup/{name}/{ts}/. {root}/ && echo dikembalikan"
        code, out, err = await sites.owner_run(st, script)
        return f"exit_code: {code}\n{out}{err}"

    async def host_service(ctx: ToolContext, action: str, service: str) -> str:
        if action not in ("status", "reload", "restart", "test") or not SERVICE.match(service):
            return "Error: action status|reload|restart|test, service nginx|apache2|phpX.Y-fpm."
        if action == "test":
            script = f"sudo -n {'nginx' if service == 'nginx' else 'apache2ctl'} -t"
        elif action == "status":
            script = f"systemctl status --no-pager -n 20 {service}"
        else:
            if ctx.is_job:
                return "Ditolak: restart layanan tak boleh dari jadwal."
            if not await ask(ctx, f"🔄 SERVER · systemctl {action} {service}?"):
                return "User menolak."
            script = f"sudo -n systemctl {action} {service}"
        code, out, err = await sites.host.run(sites.host.as_admin(script))
        return f"exit_code: {code}\n{out[-4000:]}{err[-2000:]}"

    s = {"site": {"type": "string", "description": "Nama situs (lihat host_sites)."}}
    path = {"path": {"type": "string", "description": "Relatif ke root situs."}}
    for name, desc, params, req, fn in [
        ("host_sites", "Daftar situs web di server user yang boleh disentuh.", {}, [], host_sites),
        ("host_run", "Jalankan perintah bash di folder situs, sebagai pemilik situs. Baca (ls, cat, tail log, "
         "git status/log/diff, php artisan route:list/about/migrate:status, curl GET) langsung jalan; "
         "yang mengubah minta persetujuan user + snapshot otomatis.",
         {**s, "command": {"type": "string"}, "timeout": {"type": "integer", "default": 120}},
         ["site", "command"], host_run),
        ("host_read", "Baca file situs dengan nomor baris. Rahasia tampil ***.",
         {**s, **path, "offset": {"type": "integer", "default": 1}, "limit": {"type": "integer", "default": 300}},
         ["site", "path"], host_read),
        ("host_edit", "Ganti potongan teks di file situs (old harus persis & unik). User melihat diff dan "
         "menyetujui. Snapshot otomatis.",
         {**s, **path, "old": {"type": "string"}, "new": {"type": "string"},
          "replace_all": {"type": "boolean", "default": False}}, ["site", "path", "old", "new"], host_edit),
        ("host_write", "Buat / timpa file di situs. User melihat diff dan menyetujui. Utamakan host_edit.",
         {**s, **path, "content": {"type": "string"}}, ["site", "path", "content"], host_write),
        ("host_reveal", "Minta izin user untuk melihat SATU nilai dari .env situs (mis. APP_URL).",
         {**s, "key": {"type": "string"}}, ["site", "key"], host_reveal),
        ("host_rollback", "Tanpa snapshot: daftar snapshot. Dengan snapshot: kembalikan file situs ke sana "
         "(minta persetujuan).", {**s, "snapshot": {"type": "string"}}, ["site"], host_rollback),
        ("host_service", "status/test/reload/restart nginx, apache2, atau phpX.Y-fpm. reload/restart minta "
         "persetujuan.", {"action": {"type": "string", "enum": ["status", "test", "reload", "restart"]},
                         "service": {"type": "string"}}, ["action", "service"], host_service),
    ]:
        reg.add(Tool(name, desc, {"type": "object", "properties": params, "required": req}, fn))
