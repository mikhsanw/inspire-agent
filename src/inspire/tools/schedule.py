"""Tool schedule: buat / lihat / hapus pengingat dan tugas berulang."""

from __future__ import annotations

import datetime as dt

from ..scheduler import Scheduler, make_trigger, parse_at
from ..store import Store
from . import Registry, Tool, ToolContext


def register_schedule(reg: Registry, store: Store, scheduler: Scheduler) -> None:
    tz = scheduler.tz

    async def schedule(
        ctx: ToolContext,
        action: str,
        name: str = "",
        prompt: str = "",
        cron: str = "",
        at: str = "",
        allow_shell: bool = False,
        id: int = 0,
    ) -> str:
        if action == "list":
            jobs = store.jobs(ctx.chat_id)
            if not jobs:
                return "Belum ada jadwal."
            return "\n".join(
                f"#{j.id} {j.name} | {j.kind} {j.spec} | berikut: {scheduler.next_run(j.id)}"
                f"{' | shell-auto' if j.allow_shell else ''}\n   prompt: {j.prompt[:150]}"
                for j in jobs
            )

        if action == "delete":
            job = store.get_job(id)
            if not job or job.chat_id != ctx.chat_id or not job.enabled:
                return f"Tak ada jadwal aktif #{id}."
            store.disable_job(id)
            scheduler.remove(id)
            return f"Jadwal #{id} '{job.name}' dihapus."

        if action != "create":
            return "Error: action harus create, list, atau delete."
        if not name or not prompt:
            return "Error: create butuh name dan prompt."
        if bool(cron) == bool(at):
            return "Error: isi tepat salah satu: cron (berulang) atau at (sekali)."

        kind, spec = ("cron", cron.strip()) if cron else ("once", at.strip())
        try:
            make_trigger(kind, spec, tz)
        except ValueError as e:
            return f"Error: format waktu salah ({e}). cron = 5 kolom 'm h dom mon dow', at = 'YYYY-MM-DD HH:MM'."
        if kind == "once" and parse_at(spec, tz) <= dt.datetime.now(tz):
            return "Error: waktu 'at' sudah lewat."

        if allow_shell:
            # Izin shell tanpa tanya = celah besar kalau diminta oleh isi web. Wajib izin user.
            if not ctx.confirm or not await ctx.confirm(
                f"Izinkan jadwal '{name}' menjalankan shell TANPA konfirmasi tiap kali?\n\n"
                f"Prompt: {prompt[:500]}"
            ):
                return "User tidak mengizinkan allow_shell. Buat ulang tanpa allow_shell."

        job = store.add_job(ctx.chat_id, name, prompt, kind, spec, allow_shell)
        scheduler.add(job)
        return f"Jadwal #{job.id} '{name}' dibuat. Jalan berikut: {scheduler.next_run(job.id)}"

    reg.add(
        Tool(
            name="schedule",
            description=(
                "Kelola jadwal. Saat jadwal jalan, kamu (agent) dijalankan dengan `prompt` "
                "lalu balasanmu dikirim ke user. Pakai untuk pengingat ('ingatkan jam 7 "
                "minum obat') dan tugas berulang ('tiap Senin 08:00 rangkum berita AI'). "
                "`at` = sekali, format 'YYYY-MM-DD HH:MM' zona user. `cron` = berulang, "
                "5 kolom 'menit jam tgl bulan hari' (hari: 0=Minggu atau mon-sun). "
                "Tulis prompt lengkap & mandiri, mis. 'Ingatkan user minum obat pagi.'"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "list", "delete"]},
                    "name": {"type": "string", "description": "Judul singkat."},
                    "prompt": {"type": "string", "description": "Instruksi untuk dirimu saat jadwal jalan."},
                    "cron": {"type": "string", "description": "Mis. '0 7 * * *' = tiap hari 07:00."},
                    "at": {"type": "string", "description": "Mis. '2026-10-18 07:00'."},
                    "allow_shell": {
                        "type": "boolean",
                        "description": "Shell tanpa konfirmasi saat job jalan. Butuh izin user. Default false.",
                    },
                    "id": {"type": "integer", "description": "Untuk delete."},
                },
                "required": ["action"],
            },
            fn=schedule,
        )
    )
