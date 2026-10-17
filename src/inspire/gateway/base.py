"""Interface gateway. Agent tidak tahu ia di Telegram atau Discord."""

from __future__ import annotations

from abc import ABC, abstractmethod


class Gateway(ABC):
    @abstractmethod
    def run(self) -> None:
        """Jalan terus (blocking) sampai dihentikan."""

    @abstractmethod
    async def send(self, chat_id: str, text: str) -> None:
        """Kirim pesan duluan (dipakai jadwal di fase 2)."""


def split_text(text: str, size: int) -> list[str]:
    """Pecah teks panjang, utamakan potong di baris kosong / baris baru."""
    chunks = []
    while len(text) > size:
        cut = text.rfind("\n\n", 0, size)
        if cut < size // 2:
            cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks
