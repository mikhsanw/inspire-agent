import asyncio
from types import SimpleNamespace

import pytest

from inspire import Router
from inspire.gateway.base import command, safe_name
from inspire.skills import Skills
from inspire.tools import ToolContext
from inspire.tools.skill import register_skill

from test_core import make


def test_skills_crud_git_undo(tmp_path):
    sk = Skills(tmp_path / "skills")
    assert sk.save("Bad Name", "d", "b", create=True).startswith("Error")
    assert "dibuat" in sk.save("cek-ssl", "Cek masa berlaku SSL domain", "1. openssl ...", create=True)
    assert sk.save("cek-ssl", "x", "y", create=True).startswith("Error: skill 'cek-ssl' sudah ada")
    assert "diperbarui" in sk.save("cek-ssl", "Cek SSL", "langkah baru", create=False)
    assert sk.get("cek-ssl").body == "langkah baru"
    assert sk.index() == "- cek-ssl: Cek SSL"
    hist = sk.history()
    assert "create: cek-ssl" in hist and "update: cek-ssl" in hist

    assert "Dibatalkan: " in sk.undo()          # balik ke versi pertama
    assert sk.get("cek-ssl").body == "1. openssl ..."
    assert "dihapus" in sk.delete("cek-ssl")
    assert sk.get("cek-ssl") is None
    sk.undo()                                    # hapus pun bisa dibatalkan
    assert sk.get("cek-ssl") is not None


def test_skills_undo_first_commit(tmp_path):
    sk = Skills(tmp_path / "s")
    sk.save("satu", "d", "b", create=True)
    assert "Dibatalkan" in sk.undo()
    assert sk.all() == []


async def test_skill_tool_notifies_and_shows_in_next_session(tmp_path):
    agent, llm = make(tmp_path, [{"role": "assistant", "content": "ok"}])
    sk = Skills(tmp_path / "skills")
    agent.skills = sk
    register_skill(agent.tools, sk)
    sent = []

    async def notify(chat_id, text):
        sent.append((chat_id, text))

    sk.notify = notify
    out = await agent.tools.call("skill", {
        "action": "create", "name": "rangkum-berita", "description": "Cara rangkum berita harian",
        "body": "1. web_search\n2. fetch_url 3 sumber\n3. ringkas",
    }, ToolContext("42", 1))
    assert "dibuat" in out
    assert sent and sent[0][0] == "42" and "rangkum-berita" in sent[0][1]

    view = await agent.tools.call("skill", {"action": "view", "name": "rangkum-berita"}, ToolContext("42", 1))
    assert "fetch_url 3 sumber" in view

    # update tanpa description → description lama dipakai
    await agent.tools.call("skill", {"action": "update", "name": "rangkum-berita", "body": "baru"},
                           ToolContext("42", 1))
    assert sk.get("rangkum-berita").description == "Cara rangkum berita harian"

    await agent.handle("42", "halo")
    assert "rangkum-berita: Cara rangkum berita harian" in llm.seen[-1][0]["content"]


async def test_shared_commands(tmp_path):
    agent, _ = make(tmp_path, [])
    agent.skills = Skills(tmp_path / "skills")
    assert "!model" in await command(agent, "dc:1", "help", [], "!")
    assert "Sesi baru" in await command(agent, "1", "new", [], "/")
    assert "▶ fake" in await command(agent, "1", "model", [], "/")
    assert "Tak ada model" in await command(agent, "1", "model", ["nope"], "/")
    assert "belum ada skill" in await command(agent, "1", "skills", [], "/")
    assert "tak dikenal" in await command(agent, "1", "xyz", [], "/")


async def test_router_picks_gateway():
    class G:
        def __init__(self, prefix):
            self.prefix, self.sent = prefix, []

        def owns(self, chat_id):
            from inspire.gateway.base import Gateway
            return Gateway.owns(self, chat_id)

        async def send(self, chat_id, text):
            self.sent.append(chat_id)

        async def confirm(self, chat_id, text):
            return self.prefix == "dc:"

    tg, dc = G(""), G("dc:")
    r = Router([tg, dc])
    await r.send("123", "a")
    await r.send("dc:999", "b")
    assert tg.sent == ["123"] and dc.sent == ["dc:999"]
    assert await r.confirm_for("dc:1")("x") is True
    assert await r.confirm_for("5")("x") is False


async def test_search_history_across_gateways(tmp_path):
    agent, _ = make(tmp_path, [])
    st = agent.store
    s1 = st.new_session("123")
    st.add_message(s1.id, "123", {"role": "user", "content": "kopi favoritku arabika gayo"})
    out = await agent.tools.call("search_history", {"query": "gayo"}, ToolContext("dc:9", 1))
    assert "gayo" in out and "telegram" in out


async def test_discord_on_message(tmp_path, monkeypatch):
    from inspire.gateway import discord as dg

    agent, llm = make(tmp_path, [{"role": "assistant", "content": "halo juga"}])
    gw = dg.DiscordGateway("tok", [7], agent)
    gw.client = SimpleNamespace(user=SimpleNamespace(id=1))
    replies = []

    class Typing:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    def msg(content, author=7, guild=None, mentions=()):
        async def reply(text, mention_author=False):
            replies.append(text)
        return SimpleNamespace(
            author=SimpleNamespace(id=author, bot=False), guild=guild, mentions=list(mentions),
            content=content, attachments=[], reply=reply,
            channel=SimpleNamespace(id=55, typing=lambda: Typing(), send=reply),
        )

    await gw.on_message(msg("!help"))
    assert "!new" in replies[-1]
    await gw.on_message(msg("halo"))
    assert replies[-1] == "halo juga"
    assert llm.seen[-1][-1]["content"].endswith("halo")
    assert agent.store.current_session("dc:55") is not None

    n = len(replies)
    await gw.on_message(msg("halo", author=8))               # orang asing
    await gw.on_message(msg("halo", guild=object()))         # server tanpa mention
    assert len(replies) == n


def test_safe_name():
    assert safe_name("../../etc/pass wd.pdf") == "pass_wd.pdf"
