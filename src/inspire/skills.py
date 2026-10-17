"""Skill: resep kerja yang agent tulis sendiri. Disimpan sebagai markdown, di-git."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

Notify = Callable[[str, str], Awaitable[None]]
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")


@dataclass
class Skill:
    name: str
    description: str
    body: str

    def render(self) -> str:
        return f"---\nname: {self.name}\ndescription: {self.description}\n---\n\n{self.body.strip()}\n"


def parse(text: str, fallback_name: str) -> Skill:
    meta, body = {}, text
    if text.startswith("---"):
        _, head, body = text.split("---", 2)
        for line in head.strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    return Skill(meta.get("name", fallback_name), meta.get("description", ""), body.strip())


class Skills:
    def __init__(self, root: Path, max_body: int = 8000):
        self.root = root
        self.max_body = max_body
        self.notify: Notify | None = None  # diisi main → pesan ke user tiap perubahan
        self.root.mkdir(parents=True, exist_ok=True)
        self.git_ok = shutil.which("git") is not None
        if self.git_ok and not (root / ".git").exists():
            self._git("init", "-q")
            self._git("config", "user.name", "inspire")
            self._git("config", "user.email", "inspire@localhost")
        if not self.git_ok:
            log.warning("git tak ada → perubahan skill tidak diversikan")

    # --- git ------------------------------------------------------------

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *args], capture_output=True, text=True
        )

    def _commit(self, message: str) -> None:
        if not self.git_ok:
            return
        self._git("add", "-A")
        r = self._git("commit", "-q", "-m", message)
        if r.returncode not in (0, 1):  # 1 = tak ada perubahan
            log.warning("git commit skill gagal: %s", r.stderr.strip())

    def history(self, n: int = 10) -> str:
        if not self.git_ok:
            return "git tak tersedia."
        r = self._git("log", f"-{n}", "--format=%h %ad %s", "--date=format:%m-%d %H:%M")
        return r.stdout.strip() or "Belum ada riwayat."

    def undo(self) -> str:
        """Batalkan perubahan skill terakhir (git revert, riwayat tetap utuh)."""
        if not self.git_ok:
            return "git tak tersedia."
        last = self._git("log", "-1", "--format=%h %s").stdout.strip()
        if not last:
            return "Belum ada perubahan untuk dibatalkan."
        if self._git("rev-list", "--count", "HEAD").stdout.strip() == "1":
            # Commit pertama tak bisa di-revert biasa; hapus isinya saja.
            for f in self.root.glob("*.md"):
                f.unlink()
            self._commit(f"undo: {last}")
        else:
            r = self._git("revert", "--no-edit", "HEAD")
            if r.returncode != 0:
                return f"Gagal undo: {r.stderr.strip()[:200]}"
        return f"Dibatalkan: {last}"

    # --- CRUD -----------------------------------------------------------

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.md"

    def all(self) -> list[Skill]:
        return [parse(p.read_text(encoding="utf-8"), p.stem) for p in sorted(self.root.glob("*.md"))]

    def get(self, name: str) -> Skill | None:
        p = self._path(name)
        return parse(p.read_text(encoding="utf-8"), name) if p.exists() else None

    def index(self) -> str:
        """Untuk system prompt: cuma nama + deskripsi, isi dibaca saat perlu."""
        items = self.all()
        if not items:
            return "(belum ada skill)"
        return "\n".join(f"- {s.name}: {s.description}" for s in items)

    def save(self, name: str, description: str, body: str, create: bool) -> str:
        if not NAME_RE.match(name):
            return "Error: name harus huruf kecil/angka/strip, 2–49 karakter (mis. 'cek-ssl-domain')."
        exists = self._path(name).exists()
        if create and exists:
            return f"Error: skill '{name}' sudah ada. Pakai action=update."
        if not create and not exists:
            return f"Error: skill '{name}' tidak ada."
        if not description.strip() or not body.strip():
            return "Error: description dan body wajib diisi."
        if len(body) > self.max_body:
            return f"Error: body {len(body)} karakter > batas {self.max_body}. Ringkas."
        description = " ".join(description.split())[:200]
        self._path(name).write_text(Skill(name, description, body).render(), encoding="utf-8")
        self._commit(f"{'create' if create else 'update'}: {name}")
        return f"Skill '{name}' {'dibuat' if create else 'diperbarui'}."

    def delete(self, name: str) -> str:
        p = self._path(name)
        if not p.exists():
            return f"Error: skill '{name}' tidak ada."
        p.unlink()
        self._commit(f"delete: {name}")
        return f"Skill '{name}' dihapus."
