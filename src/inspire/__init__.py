"""inspire — agent AI pribadi ala Hermes."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class App:
    cfg: "Config"
    agent: "Agent"
    scheduler: "Scheduler"
    skills: "Skills"


def build(config_path: str | Path) -> App:
    from dotenv import load_dotenv

    from . import config as config_mod
    from .agent import Agent
    from .llm import LLM
    from .media import Media
    from .memory import Memory
    from .scheduler import Scheduler
    from .skills import Skills
    from .store import Store
    from .tools import Registry
    from .tools.core import register_core
    from .tools.schedule import register_schedule
    from .tools.shell import register_shell
    from .tools.skill import register_skill
    from .tools.video import register_video
    from .tools.web import register_web

    load_dotenv()
    cfg = config_mod.load(config_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    store = Store(cfg.data_dir / "inspire.db")
    memory = Memory(cfg.data_dir, cfg.memory_chars, cfg.user_chars)
    llm = LLM(cfg.models, cfg.default_model, cfg.fallback, cfg.request_timeout)
    media = Media(cfg.media)
    skills = Skills(cfg.data_dir / "skills")

    tools = Registry()
    register_core(tools, memory, store)
    register_web(tools, cfg.fetch_max_chars, cfg.search_backends)
    register_video(tools, media)
    register_skill(tools, skills)
    if cfg.sandbox_url:
        register_shell(tools, cfg.sandbox_url, cfg.sandbox_token, cfg.shell_timeout)
    else:
        log.warning("shell.sandbox_url kosong → tool shell mati")

    agent = Agent(cfg, store, memory, llm, tools, media, skills)
    scheduler = Scheduler(store, agent)
    register_schedule(tools, store, scheduler)
    return App(cfg, agent, scheduler, skills)


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

    def confirm_for(self, chat_id: str):
        g = self._gw(chat_id)
        return lambda text: g.confirm(chat_id, text)


async def run(app: App) -> None:
    cfg = app.cfg
    gateways = []
    if cfg.telegram_token:
        from .gateway.telegram import TelegramGateway

        gateways.append(
            TelegramGateway(
                cfg.telegram_token, cfg.allowed_user_ids, app.agent, cfg.confirm_timeout, cfg.inbox_dir
            )
        )
    if cfg.discord_token:
        from .gateway.discord import DiscordGateway

        gateways.append(
            DiscordGateway(
                cfg.discord_token,
                cfg.discord_user_ids,
                app.agent,
                cfg.confirm_timeout,
                cfg.inbox_dir,
                cfg.discord_prefix,
            )
        )
    if not gateways:
        raise SystemExit("Isi minimal satu: TELEGRAM_BOT_TOKEN atau DISCORD_BOT_TOKEN.")

    router = Router(gateways)
    app.scheduler.attach(router.send, router.confirm_for)
    app.skills.notify = router.send

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    started = []
    try:
        for g in gateways:
            await g.start()
            started.append(g)
        app.scheduler.start()
        log.info("inspire jalan: %s", ", ".join(type(g).__name__ for g in gateways))
        await stop.wait()
    finally:
        log.info("berhenti…")
        with contextlib.suppress(Exception):
            app.scheduler.sched.shutdown(wait=False)
        for g in started:
            await g.stop()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "yt_dlp", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # discord.py peringatkan soal fitur voice channel yang memang tak dipakai.
    logging.getLogger("discord").setLevel(logging.ERROR)
    app = build(os.environ.get("INSPIRE_CONFIG", "config.yaml"))
    asyncio.run(run(app))
