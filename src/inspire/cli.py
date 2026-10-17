"""Perintah terminal: inspire [run|setup|doctor|pair]."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path


def _logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "yt_dlp", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # discord.py peringatkan soal fitur voice channel yang memang tak dipakai.
    logging.getLogger("discord").setLevel(logging.ERROR)


def _settings():
    from .settings import Settings

    s = Settings()
    s.migrate_legacy(Path.cwd())
    return s


def cmd_run(_: argparse.Namespace) -> int:
    from . import build, run

    _logging()
    restart = asyncio.run(run(build(_settings())))
    if restart:
        logging.getLogger("inspire").info("restart…")
        # Ganti proses dengan yang baru: jalan sama di Docker, systemd, maupun terminal.
        os.execv(sys.executable, [sys.executable, "-m", "inspire", "run"])
    return 0


def cmd_setup(a: argparse.Namespace) -> int:
    from .setup.wizard import wizard

    logging.basicConfig(level=logging.ERROR)
    wizard(_settings(), a.bagian)
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    from .setup.wizard import doctor

    logging.basicConfig(level=logging.ERROR)
    return doctor(_settings())


def cmd_pair(_: argparse.Namespace) -> int:
    from .setup import checks

    s = _settings()
    code = s.pairing_code()
    tg, dc = s.owners("telegram"), s.owners("discord")
    if tg:
        print(f"Telegram sudah punya pemilik: {tg}")
    elif s.key("TELEGRAM_BOT_TOKEN"):
        good, username = asyncio.run(checks.telegram(s.key("TELEGRAM_BOT_TOKEN")))
        link = f"https://t.me/{username.lstrip('@')}?start={code}" if good else ""
        print(f"Telegram: kirim /pair {code} ke bot" + (f"\n  atau buka {link}" if link else ""))
    if dc:
        print(f"Discord sudah punya pemilik: {dc}")
    elif s.key("DISCORD_BOT_TOKEN"):
        print(f"Discord: DM bot dengan !pair {code}")
    if not (s.key("TELEGRAM_BOT_TOKEN") or s.key("DISCORD_BOT_TOKEN")):
        print("Belum ada token bot. Jalankan: inspire setup")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="inspire", description="Agent AI pribadi ala Hermes.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run", help="jalankan bot (bawaan)").set_defaults(fn=cmd_run)
    sp = sub.add_parser("setup", help="atur dari terminal; tanpa bagian = wizard Cepat/Lengkap")
    sp.add_argument(
        "bagian", nargs="?",
        help="telegram, pair, model, suara, gambar, discord, shell, youtube, zona",
    )
    sp.set_defaults(fn=cmd_setup)
    sub.add_parser("doctor", help="cek semua fitur & key").set_defaults(fn=cmd_doctor)
    sub.add_parser("pair", help="tampilkan kode pairing").set_defaults(fn=cmd_pair)
    a = p.parse_args()
    sys.exit(getattr(a, "fn", cmd_run)(a) or 0)


if __name__ == "__main__":
    main()
