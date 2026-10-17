"""Jadwal: job disimpan di SQLite, dijalankan APScheduler di event loop bot."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .agent import Agent
from .store import Job, Store
from .tools import Confirm

log = logging.getLogger(__name__)

Deliver = Callable[[str, str], Awaitable[None]]
ConfirmFor = Callable[[str], Confirm]


def parse_at(text: str, tz: ZoneInfo) -> dt.datetime:
    """'2026-10-18 07:00' / ISO → datetime ber-zona. ValueError kalau rusak."""
    when = dt.datetime.fromisoformat(text.strip().replace("T", " "))
    return when if when.tzinfo else when.replace(tzinfo=tz)


def make_trigger(kind: str, spec: str, tz: ZoneInfo):
    if kind == "cron":
        return CronTrigger.from_crontab(spec, timezone=tz)
    return DateTrigger(run_date=parse_at(spec, tz), timezone=tz)


class Scheduler:
    def __init__(self, store: Store, agent: Agent):
        self.store = store
        self.agent = agent
        self.tz = agent.tz
        self.sched = AsyncIOScheduler(timezone=self.tz)
        self.deliver: Deliver | None = None
        self.confirm_for: ConfirmFor | None = None

    def attach(self, deliver: Deliver, confirm_for: ConfirmFor | None = None) -> None:
        self.deliver = deliver
        self.confirm_for = confirm_for

    def start(self) -> None:
        """Harus dipanggil di dalam event loop yang sedang jalan."""
        now = dt.datetime.now(self.tz)
        for job in self.store.jobs():
            if job.kind == "once" and parse_at(job.spec, self.tz) < now - dt.timedelta(hours=1):
                log.info("job #%s lewat waktu, dimatikan", job.id)
                self.store.disable_job(job.id)
                continue
            self.add(job)
        self.sched.start()
        log.info("scheduler jalan, %d job", len(self.sched.get_jobs()))

    def add(self, job: Job) -> None:
        self.sched.add_job(
            self._fire,
            trigger=make_trigger(job.kind, job.spec, self.tz),
            args=[job.id],
            id=str(job.id),
            replace_existing=True,
            misfire_grace_time=3600,  # bot mati sebentar → tetap jalan saat hidup lagi
            coalesce=True,
        )

    def remove(self, job_id: int) -> None:
        if self.sched.get_job(str(job_id)):
            self.sched.remove_job(str(job_id))

    def next_run(self, job_id: int) -> str:
        j = self.sched.get_job(str(job_id))
        # Sebelum scheduler start, job belum punya next_run_time → hitung dari trigger.
        t = j and j.trigger.get_next_fire_time(None, dt.datetime.now(self.tz))
        return t.strftime("%Y-%m-%d %H:%M %a") if t else "-"

    async def _fire(self, job_id: int) -> None:
        job = self.store.get_job(job_id)
        if not job or not job.enabled:
            return
        log.info("job #%s '%s' jalan", job.id, job.name)
        confirm = self.confirm_for(job.chat_id) if self.confirm_for else None
        try:
            result = await self.agent.run_job(job, confirm)
        except Exception as e:
            log.exception("job #%s gagal", job.id)
            result = f"⚠️ Job gagal: {type(e).__name__}: {e}"
        text = f"⏰ {job.name}\n\n{result}"
        self.store.job_ran(job.id, result)
        if job.kind == "once":
            self.store.disable_job(job.id)
        if self.deliver:
            await self.deliver(job.chat_id, text)
            await self.agent.note_delivered(job.chat_id, text)
