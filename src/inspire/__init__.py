"""inspire — agent AI pribadi ala Hermes."""

from __future__ import annotations

import logging
import os
from pathlib import Path


def build(config_path: str | Path):
    from dotenv import load_dotenv

    from . import config as config_mod
    from .agent import Agent
    from .llm import LLM
    from .memory import Memory
    from .store import Store
    from .tools import Registry
    from .tools.core import register_core

    load_dotenv()
    cfg = config_mod.load(config_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    store = Store(cfg.data_dir / "inspire.db")
    memory = Memory(cfg.data_dir, cfg.memory_chars, cfg.user_chars)
    llm = LLM(cfg.models, cfg.default_model, cfg.fallback, cfg.request_timeout)
    tools = Registry()
    register_core(tools, memory, store)
    return cfg, Agent(cfg, store, memory, llm, tools)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from .gateway.telegram import TelegramGateway

    cfg, agent = build(os.environ.get("INSPIRE_CONFIG", "config.yaml"))
    TelegramGateway(cfg.telegram_token, cfg.allowed_user_ids, agent).run()
