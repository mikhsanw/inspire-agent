"""inspire — agent AI pribadi ala Hermes."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class App:
    settings: "Settings"
    cfg: "Config"
    agent: "Agent"
    scheduler: "Scheduler"
    skills: "Skills"
    outbox: "Outbox" = None
    grants: "Grants" = None
    journal: "Journal" = None
    tasks: "Tasks" = None
    stop: asyncio.Event | None = None
    restart_requested: bool = False
    gateways: list = field(default_factory=list)

    def reload(self) -> None:
        """Setelan berubah → terapkan yang bisa langsung (model, key, media)."""
        from .config import from_settings

        new = from_settings(self.settings)
        self.cfg.__dict__.update(new.__dict__)  # objek sama dipakai agent & tool
        self.agent.llm.update(new.models, new.default_model, new.fallback)
        if self.agent.media:
            self.agent.media.cfg = new.media
        for g in self.gateways:
            g.refresh_owners()

    def request_restart(self, notify_chat: str | None = None) -> None:
        if notify_chat:
            self.settings.data["restart_notify"] = notify_chat
            self.settings.save()
        self.restart_requested = True
        if self.stop:
            self.stop.set()


def build(settings: "Settings | None" = None) -> App:
    from .agent import Agent
    from .config import from_settings
    from .journal import Journal
    from .llm import LLM
    from .media import Media
    from .memory import Memory
    from .scheduler import Scheduler
    from .tasks import Tasks
    from .settings import Settings
    from .skills import Skills
    from .store import Store
    from .tools import Registry
    from .tools.core import register_core, register_journal
    from .tools.files import Outbox, Sandbox, register_files
    from .tools.schedule import register_schedule
    from .tools.shell import Grants, register_shell
    from .tools.skill import register_skill
    from .tools.video import register_video
    from .tools.web import register_web

    if settings is None:
        settings = Settings()
        settings.migrate_legacy(Path.cwd())
    cfg = from_settings(settings)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    store = Store(cfg.data_dir / "inspire.db")
    memory = Memory(cfg.data_dir, cfg.memory_chars, cfg.user_chars)
    llm = LLM(cfg.models, cfg.default_model, cfg.fallback, cfg.request_timeout, cfg.chain_timeout)
    media = Media(cfg.media)
    skills = Skills(cfg.data_dir / "skills")

    journal = Journal(store.db)
    tools = Registry()
    register_core(tools, memory, store)
    register_journal(tools, journal)
    register_web(tools, cfg.fetch_max_chars, cfg.search_backends)
    register_video(tools, media)
    register_skill(tools, skills)
    outbox, grants = Outbox(), Grants()
    if cfg.sandbox_url and settings.get("shell.enabled") is not False:
        register_shell(tools, cfg.sandbox_url, cfg.sandbox_token, cfg.shell_timeout, grants, journal)
        journal.sandbox = Sandbox(cfg.sandbox_url, cfg.sandbox_token)
        register_files(tools, journal.sandbox, outbox, journal)
    else:
        log.info("shell mati (butuh mode Docker / dimatikan di setup)")

    ready = [x for x in cfg.sites if x.get("owner")]  # owner terisi = langkah sudo selesai
    if cfg.host_address and ready and cfg.host_key and Path(cfg.host_key).exists():
        from .tools.db import register_db
        from .tools.host import Host, Sites, register_host

        host = Host(cfg.host_address, cfg.host_port, cfg.host_user, cfg.host_key,
                    str(Path(cfg.host_key).parent / "known_hosts"))
        sites = Sites(host, ready, journal)
        journal.sites = sites
        register_host(tools, sites)
        register_db(tools, sites)

    agent = Agent(cfg, store, memory, llm, tools, media, skills)
    scheduler = Scheduler(store, agent)
    register_schedule(tools, store, scheduler)
    tasks = Tasks(store.db, agent, journal.sandbox, journal, cfg) if journal.sandbox else None
    return App(settings, cfg, agent, scheduler, skills, outbox, grants, journal, tasks)


class Router:
    """Kirim ke gateway yang memiliki chat_id ("dc:…" → Discord, angka → Telegram)."""

    def __init__(self, gateways: list):
        self.gateways = gateways

    def _gw(self, chat_id: str):
        for g in self.gateways:
            if g.owns(chat_id):
                return g
        raise LookupError(f"tak ada gateway untuk chat {chat_id}")

    async def send(self, chat_id: str, text: str) -> None:
        await self._gw(chat_id).send(chat_id, text)

    async def send_file(self, chat_id: str, name: str, data: bytes, caption: str = "") -> None:
        await self._gw(chat_id).send_file(chat_id, name, data, caption)

    async def send_code(self, chat_id: str, text: str) -> None:
        await self._gw(chat_id).send_code(chat_id, text)

    def confirm_for(self, chat_id: str):
        g = self._gw(chat_id)
        return lambda text, **kw: g.confirm(chat_id, text, **kw)


async def run(app: App) -> bool:
    """Jalan sampai dihentikan. True = minta restart (dipanggil ulang oleh cli)."""
    cfg = app.cfg
    if cfg.telegram_token:
        from .gateway.telegram import TelegramGateway

        app.gateways.append(TelegramGateway(app))
    if cfg.discord_token:
        from .gateway.discord import DiscordGateway

        app.gateways.append(DiscordGateway(app))
    if not app.gateways:
        raise SystemExit(
            "Belum ada gateway. Jalankan `inspire setup` (isi token Telegram atau Discord)."
        )

    router = Router(app.gateways)
    app.scheduler.attach(router.send, router.confirm_for)
    app.skills.notify = router.send
    app.outbox.send = router.send_file
    if app.tasks:
        app.tasks.router = router

    app.stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, app.stop.set)

    started = []
    try:
        for g in app.gateways:
            await g.start()
            started.append(g)
        app.scheduler.start()
        log.info("inspire jalan: %s", ", ".join(type(g).__name__ for g in app.gateways))
        if any(not g.owner_ids() for g in app.gateways):
            log.warning(
                "BELUM ADA PEMILIK. Kirim ke bot: /pair %s (Telegram) atau !pair %s (DM Discord)",
                app.settings.pairing_code(), app.settings.pairing_code(),
            )
        if chat := app.settings.data.pop("restart_notify", None):
            app.settings.save()
            with contextlib.suppress(Exception):
                await router.send(chat, "✅ inspire aktif lagi.")
        await app.stop.wait()
    finally:
        log.info("berhenti…")
        with contextlib.suppress(Exception):
            app.scheduler.sched.shutdown(wait=False)
        for g in started:
            await g.stop()
    return app.restart_requested
