import json

import pytest

from inspire.agent import Agent
from inspire.config import Config, ModelCfg, load
from inspire.gateway.base import split_text
from inspire.memory import Memory
from inspire.store import Store
from inspire.tools import Registry, ToolContext
from inspire.tools.core import register_core


class FakeLLM:
    """Balas sesuai skrip; catat pesan yang diterima."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []
        self.models = {"fake": ModelCfg(model="fake")}
        self.active, self.fallback = "fake", []

    def set_active(self, name):
        return name in self.models

    def status(self, name):
        return "ok", ""

    def reset(self, name=None):
        pass

    async def complete(self, messages, tools=None):
        self.seen.append(messages)
        if tools is None:  # panggilan ringkas
            return {"role": "assistant", "content": "RINGKAS"}, "fake"
        return self.replies.pop(0), "fake"


def make(tmp_path, replies, **over):
    cfg = Config(
        data_dir=tmp_path, telegram_token="x", allowed_user_ids=[1],
        models={"fake": ModelCfg(model="fake")}, default_model="fake", fallback=[],
        **over,
    )
    store = Store(tmp_path / "t.db")
    memory = Memory(tmp_path, cfg.memory_chars, cfg.user_chars)
    reg = Registry()
    register_core(reg, memory, store)
    llm = FakeLLM(replies)
    return Agent(cfg, store, memory, llm, reg), llm


def call(name, args, id="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    ]}


def test_memory_limit_and_ops(tmp_path):
    m = Memory(tmp_path, memory_chars=30, user_chars=30).files["user"]
    assert m.add("suka kopi hitam").startswith("Disimpan")
    assert m.add("x" * 40).startswith("Ditolak")
    assert m.replace("kopi", "suka teh").startswith("Diganti")
    assert m.entries() == ["suka teh"]
    assert m.remove("teh").startswith("Dihapus")
    assert m.entries() == []


def test_fts_search(tmp_path):
    st = Store(tmp_path / "t.db")
    s = st.new_session("9")
    st.add_message(s.id, "9", {"role": "user", "content": "kucingku namanya Mochi"})
    st.add_message(s.id, "9", {"role": "assistant", "content": "lucu"})
    hits = st.search("Mochi", "9")
    assert len(hits) == 1 and "[Mochi]" in hits[0]["snip"]
    assert st.search("Mochi", "other") == []
    # input aneh tidak boleh bikin error sintaks FTS
    assert st.search('"AND OR (', "9") == []


async def test_agent_tool_loop_saves_memory(tmp_path):
    agent, llm = make(tmp_path, [
        call("memory", {"action": "add", "target": "user", "text": "Nama: Ikhsan"}),
        {"role": "assistant", "content": "Oke, kuingat."},
    ])
    out = await agent.handle("1", "namaku Ikhsan")
    assert out == "Oke, kuingat."
    assert "Nama: Ikhsan" in agent.memory.files["user"].render()
    # putaran kedua model lihat hasil tool
    assert llm.seen[1][-1]["role"] == "tool"
    # snapshot dibekukan: sesi yang sama belum lihat memori baru
    assert "Nama: Ikhsan" not in llm.seen[1][0]["content"]
    # sesi baru lihat
    agent.new_session("1")
    llm.replies.append({"role": "assistant", "content": "hai"})
    await agent.handle("1", "halo")
    assert "Nama: Ikhsan" in llm.seen[-1][0]["content"]


async def test_bad_tool_json_reported(tmp_path):
    reg = Registry()
    register_core(reg, Memory(tmp_path, 100, 100), Store(tmp_path / "t.db"))
    out = await reg.call("memory", "{rusak", ToolContext("1", 1))
    assert out.startswith("Error: argumen bukan JSON")
    assert (await reg.call("nope", "{}", ToolContext("1", 1))).startswith("Error: tool")


async def test_compression(tmp_path):
    agent, llm = make(tmp_path, [], max_context_tokens=600, keep_last=2)
    for i in range(6):
        llm.replies.append({"role": "assistant", "content": f"jawab {i} " + "z" * 100})
        await agent.handle("1", f"tanya {i} " + "y" * 100)
    s = agent.store.current_session("1")
    assert s.summary
    last_system = llm.seen[-1][0]["content"]
    assert "RINGKASAN OBROLAN" in last_system
    assert len(agent.store.context_messages(s)) < 12


def test_config_load(tmp_path, monkeypatch):
    monkeypatch.setenv("TG", "tok")
    monkeypatch.setenv("IDS", "11, 22")
    p = tmp_path / "c.yaml"
    p.write_text("""
telegram: {token: "${TG}", allowed_user_ids: "${IDS}"}
models:
  default: a
  fallback: [a, b, ghost]
  list:
    a: {model: deepseek/deepseek-chat}
    b: {model: openai/x, api_base: http://h}
""")
    cfg = load(p)
    assert cfg.telegram_token == "tok"
    assert cfg.allowed_user_ids == [11, 22]
    assert cfg.fallback == ["a", "b"]


def test_split_text():
    parts = split_text("a" * 50 + "\n\n" + "b" * 50, 60)
    assert parts == ["a" * 50, "b" * 50]
    assert all(len(p) <= 10 for p in split_text("x" * 35, 10))
