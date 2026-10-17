"""Tool skill: agent menyimpan dan memakai resep kerjanya sendiri."""

from __future__ import annotations

import logging

from ..skills import Skills
from . import Registry, Tool, ToolContext

log = logging.getLogger(__name__)


def register_skill(reg: Registry, skills: Skills) -> None:
    async def tell(ctx: ToolContext, text: str) -> None:
        if skills.notify:
            try:
                await skills.notify(ctx.chat_id, text)
            except Exception:
                log.exception("notif skill gagal")

    async def skill(
        ctx: ToolContext,
        action: str,
        name: str = "",
        description: str = "",
        body: str = "",
    ) -> str:
        if action == "list":
            return skills.index()
        if action == "view":
            s = skills.get(name)
            return s.render() if s else f"Skill '{name}' tidak ada."
        if action in ("create", "update"):
            old = skills.get(name) if action == "update" else None
            result = skills.save(
                name,
                description or (old.description if old else ""),
                body or (old.body if old else ""),
                create=action == "create",
            )
            if not result.startswith("Error"):
                icon = "🧠 Skill baru" if action == "create" else "✏️ Skill diperbarui"
                await tell(
                    ctx,
                    f"{icon}: {name}\n{description or (old and old.description) or ''}\n\n"
                    "(/skills untuk lihat, /skillundo untuk batalkan)",
                )
            return result
        if action == "delete":
            result = skills.delete(name)
            if not result.startswith("Error"):
                await tell(ctx, f"🗑 Skill dihapus: {name}\n(/skillundo untuk kembalikan)")
            return result
        return "Error: action harus list, view, create, update, atau delete."

    reg.add(
        Tool(
            name="skill",
            description=(
                "Resep kerja yang bisa dipakai ulang (procedural memory). Daftar skill ada "
                "di system prompt; `view` untuk baca isinya sebelum mengerjakan tugas yang "
                "cocok. `create` setelah kamu berhasil menyelesaikan tugas yang sulit / "
                "banyak langkah / sempat salah jalan, atau user mengajarkan cara kerja. "
                "Body: langkah-langkah konkret, perintah persis, jebakan yang ditemui, cara "
                "cek hasil. `update` kalau skill ternyata salah atau kurang. Jangan buat skill "
                "untuk hal sepele atau fakta (itu tempatnya memory). Jangan pernah masukkan "
                "rahasia (password, token) ke skill."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "view", "create", "update", "delete"],
                    },
                    "name": {"type": "string", "description": "slug, mis. 'cek-ssl-domain'"},
                    "description": {
                        "type": "string",
                        "description": "Satu kalimat: kapan skill ini dipakai.",
                    },
                    "body": {"type": "string", "description": "Isi markdown."},
                },
                "required": ["action"],
            },
            fn=skill,
        )
    )
