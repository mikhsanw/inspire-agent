"""Penjaga dasar: setiap modul harus bisa di-import dan gateway bisa dibuat.
(Satu string salah tulis di telegram.py pernah lolos karena tak ada test yang mengimpornya.)"""

import importlib
import pkgutil
from types import SimpleNamespace

import pytest

import inspire
from inspire import build
from inspire.settings import Settings


def all_modules():
    return [m.name for m in pkgutil.walk_packages(inspire.__path__, "inspire.") if not m.name.endswith("__main__")]


@pytest.mark.parametrize("name", all_modules())
def test_module_imports(name):
    importlib.import_module(name)


def test_gateways_construct_and_handlers_registered(tmp_path):
    s = Settings(tmp_path)
    s.set_key("TELEGRAM_BOT_TOKEN", "123:abc")
    s.set_key("DISCORD_BOT_TOKEN", "tok")
    s.add_owner("telegram", 42)
    s.add_owner("discord", 7)
    app = build(s)
    from inspire.gateway.discord import DiscordGateway
    from inspire.gateway.telegram import COMMANDS, TelegramGateway

    tg = TelegramGateway(app)
    assert tg.owner.user_ids == {42} and "tugas" in COMMANDS and "diff" in COMMANDS
    assert len(tg.tg.handlers[0]) >= 10
    assert DiscordGateway(app).allowed == {7}


async def test_telegram_pairing_reply_path(tmp_path):
    """Jalur /pair di telegram.py (yang dulu rusak)."""
    s = Settings(tmp_path)
    s.set_key("TELEGRAM_BOT_TOKEN", "123:abc")
    app = build(s)
    from inspire.gateway.telegram import TelegramGateway

    gw = TelegramGateway(app)
    app.gateways.append(gw)
    replies = []

    async def reply_text(text, **kw):
        replies.append(text)

    def update(text):
        return SimpleNamespace(effective_user=SimpleNamespace(id=9, username="x"),
                               effective_message=SimpleNamespace(text=text, reply_text=reply_text))

    code = s.pairing_code()
    await gw._stranger(update("/pair 000000" if code != "000000" else "/pair 111111"), None)
    assert "salah" in replies[-1]
    await gw._stranger(update(f"/start {code}"), None)
    assert replies[-1].startswith("✅ Terhubung!") and "\n\n" in replies[-1]
    assert s.owners("telegram") == [9] and gw.owner.user_ids == {9}
