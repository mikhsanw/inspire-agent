"""Memori jangka panjang: file markdown kecil yang agent kurasi sendiri."""

from __future__ import annotations

from pathlib import Path

SEP = "\n§\n"


class MemoryFile:
    """Daftar entri pendek di satu file, dengan batas total karakter.

    Batas sengaja ketat: memaksa agent meringkas dan membuang yang basi,
    sehingga isi file selalu muat di system prompt.
    """

    def __init__(self, path: Path, limit: int):
        self.path = path
        self.limit = limit

    def entries(self) -> list[str]:
        if not self.path.exists():
            return []
        text = self.path.read_text(encoding="utf-8").strip()
        return [e.strip() for e in text.split(SEP.strip()) if e.strip()]

    def render(self) -> str:
        return SEP.join(self.entries())

    def used(self) -> int:
        return len(self.render())

    def _save(self, entries: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(SEP.join(entries) + "\n", encoding="utf-8")

    def _check(self, entries: list[str]) -> str | None:
        size = len(SEP.join(entries))
        if size > self.limit:
            return (
                f"Ditolak: {size}/{self.limit} karakter. Ringkas atau hapus entri "
                "lama dulu (pakai replace/remove), lalu coba lagi."
            )
        return None

    def add(self, text: str) -> str:
        text = text.strip()
        if not text:
            return "Ditolak: teks kosong."
        entries = self.entries()
        if text in entries:
            return "Sudah ada."
        entries.append(text)
        if err := self._check(entries):
            return err
        self._save(entries)
        return f"Disimpan. {self.used()}/{self.limit} karakter."

    def replace(self, old: str, new: str) -> str:
        entries = self.entries()
        hits = [i for i, e in enumerate(entries) if old in e]
        if len(hits) != 1:
            return f"Ditolak: '{old}' cocok dengan {len(hits)} entri, harus tepat 1."
        entries[hits[0]] = new.strip()
        if err := self._check(entries):
            return err
        self._save(entries)
        return f"Diganti. {self.used()}/{self.limit} karakter."

    def remove(self, old: str) -> str:
        entries = self.entries()
        hits = [i for i, e in enumerate(entries) if old in e]
        if len(hits) != 1:
            return f"Ditolak: '{old}' cocok dengan {len(hits)} entri, harus tepat 1."
        entries.pop(hits[0])
        self._save(entries)
        return f"Dihapus. {self.used()}/{self.limit} karakter."


class Memory:
    def __init__(self, data_dir: Path, memory_chars: int, user_chars: int):
        self.files = {
            "memory": MemoryFile(data_dir / "MEMORY.md", memory_chars),
            "user": MemoryFile(data_dir / "USER.md", user_chars),
        }

    def snapshot(self) -> str:
        """Teks untuk system prompt. Dibekukan per sesi oleh pemanggil."""
        parts = []
        for name, f in self.files.items():
            title = "CATATAN AGENT" if name == "memory" else "TENTANG USER"
            body = f.render() or "(kosong)"
            parts.append(f"## {title} [{f.used()}/{f.limit} karakter]\n{body}")
        return "\n\n".join(parts)
