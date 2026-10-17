import asyncio
from types import SimpleNamespace

import litellm
import pytest

from inspire.config import ModelCfg, describe
from inspire.gateway.base import command
from inspire.llm import LLM, AllModelsFailed
from inspire.llm_errors import retry_after

from test_core import make


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def reply(text="halo"):
    msg = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class Server:
    """litellm.acompletion palsu: perilaku per model, dengan catatan panggilan."""

    def __init__(self, clock):
        self.clock, self.calls, self.behave, self.timeouts = clock, [], {}, []

    async def __call__(self, **kw):
        name = kw["model"]
        self.calls.append(name)
        self.timeouts.append(kw["timeout"])
        what = self.behave.get(name, "ok")
        if callable(what):
            what = what()
        if isinstance(what, Exception):
            raise what
        if what == "hang":
            await asyncio.sleep(30)
        return reply(f"dari {name}")


@pytest.fixture
def env(monkeypatch):
    clock = Clock()
    server = Server(clock)
    monkeypatch.setattr(litellm, "acompletion", server)
    models = {"a": ModelCfg(model="A"), "b": ModelCfg(model="B"), "c": ModelCfg(model="C")}
    llm = LLM(models, "a", ["a", "b", "c"], timeout=10, chain_timeout=150, clock=clock)
    llm.slack = 0.05
    return SimpleNamespace(llm=llm, server=server, clock=clock, models=models)


MSGS = [{"role": "user", "content": "hi"}]


async def test_limit_error_rests_model_and_next_call_skips_it(env):
    env.server.behave["A"] = Exception("RateLimitError: 429 Too Many Requests")
    out, used = await env.llm.complete(MSGS)
    assert used == "b" and out["content"] == "dari B" and env.server.calls == ["A", "B"]
    assert env.llm.status("a")[0] == "cooling" and "kena limit" in env.llm.status("a")[1]

    env.server.calls.clear()
    assert (await env.llm.complete(MSGS))[1] == "b"
    assert env.server.calls == ["B"]                                    # A dilewati, tak menunggu lagi

    env.clock.t += 61                                                   # masa istirahat (60 dtk) habis
    env.server.behave["A"] = "ok"
    env.server.calls.clear()
    assert (await env.llm.complete(MSGS))[1] == "a" and env.llm.status("a") == ("ok", "")


async def test_cooldown_grows_and_respects_retry_after_hint(env):
    env.server.behave["A"] = Exception("429 rate limit")
    for expected in (60, 120, 240):
        await env.llm.complete(MSGS)
        assert env.llm.health["a"].until - env.clock.t == expected      # eksponensial: 60 → 120 → 240
        env.clock.t = env.llm.health["a"].until + 1
    env.llm.reset()
    env.server.behave["A"] = Exception("429 RESOURCE_EXHAUSTED. Please retry in 28.9s")
    await env.llm.complete(MSGS)
    assert round(env.llm.health["a"].until - env.clock.t) == 29        # mengikuti saran server
    assert retry_after("Retry-After: 12") == 12 and retry_after("tidak ada") is None


async def test_blocked_models_stay_out_until_fixed(env):
    env.server.behave["A"] = Exception("AuthenticationError: Incorrect API key provided")
    await env.llm.complete(MSGS)
    assert env.llm.status("a") == ("blocked", "key ditolak")
    env.clock.t += 600                                                  # 10 menit kemudian masih dilewati
    env.server.calls.clear()
    await env.llm.complete(MSGS)
    assert env.server.calls == ["B"]

    # user memperbaiki key → setelan berubah → catatan error model itu dihapus
    fixed = dict(env.models, a=ModelCfg(model="A", api_key="sk-baru"))
    env.llm.update(fixed, "a", ["a", "b", "c"])
    assert env.llm.status("a")[0] == "ok"
    # model lain yang tak berubah tetap ingat riwayatnya
    env.server.behave["B"] = Exception("404 model_not_found")
    env.server.behave["A"] = Exception("429")
    await env.llm.complete(MSGS)
    env.llm.update(dict(fixed), "a", ["a", "b", "c"])
    assert env.llm.status("b")[0] == "blocked" and env.llm.status("a")[0] == "cooling"


async def test_other_block_kinds_are_classified(env):
    for exc, short in [(Exception("404 model_not_found"), "nama model tak ada"),
                       (Exception("OpenCode's free tier can only be used from within OpenCode"), "diblokir penyedia"),
                       (Exception("this model does not support tools"), "tak mendukung tool")]:
        env.llm.reset()
        env.server.behave["A"] = exc
        await env.llm.complete(MSGS)
        assert env.llm.status("a") == ("blocked", short)


async def test_context_overflow_is_not_the_models_fault(env):
    env.server.behave["A"] = Exception("This model's maximum context length is 8192 tokens")
    out, used = await env.llm.complete(MSGS)
    assert used == "b" and env.llm.status("a") == ("ok", "")            # A tak diistirahatkan


async def test_all_models_failed_lists_each_reason(env):
    env.server.behave["A"] = Exception("429")
    env.server.behave["B"] = Exception("401 Unauthorized")
    env.server.behave["C"] = Exception("503 Service Unavailable")
    with pytest.raises(AllModelsFailed) as e:
        await env.llm.complete(MSGS)
    text = str(e.value)
    assert "• a: kena limit, istirahat 60 dtk" in text and "• b: key ditolak" in text
    assert "• c: server penyedia bermasalah, istirahat 30 dtk" in text
    assert [k for _, k, _ in e.value.errors] == ["limit", "key", "server"]


async def test_all_resting_still_tries_the_soonest_to_recover(env):
    for n in "abc":
        env.server.behave[n.upper()] = Exception("429")
    with pytest.raises(AllModelsFailed):
        await env.llm.complete(MSGS)
    env.llm.health["b"].until = env.clock.t + 5                         # b paling cepat pulih
    env.server.calls.clear()
    env.server.behave["B"] = "ok"
    out, used = await env.llm.complete(MSGS)                            # tak langsung menyerah
    assert used == "b" and env.server.calls == ["B"]
    assert env.llm.status("b") == ("ok", "")


async def test_hung_model_times_out_and_falls_back(env):
    env.models["a"].timeout = 0.1                                       # timeout per model
    env.server.behave["A"] = "hang"
    out, used = await env.llm.complete(MSGS)
    assert used == "b" and env.server.timeouts[0] == 0.1
    assert env.llm.status("a")[0] == "cooling" and "tak terjangkau" in env.llm.status("a")[1]


async def test_chain_has_a_total_budget(env):
    def slow_fail():
        env.clock.t += 100                                              # tiap percobaan "memakan" 100 dtk
        return Exception("timed out")

    for n in ("A", "B", "C"):
        env.server.behave[n] = slow_fail
    with pytest.raises(AllModelsFailed):
        await env.llm.complete(MSGS)
    assert env.server.calls == ["A", "B"]                               # C tak dicoba: sisa waktu rantai < 8 dtk
    assert env.server.timeouts[1] == 10 and env.server.timeouts[0] == 10


async def test_cancellation_passes_through(env):
    env.server.behave["A"] = "hang"
    task = asyncio.create_task(env.llm.complete(MSGS))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.llm.status("a") == ("ok", "")                            # /stop bukan kegagalan model


async def test_no_models_gives_setup_hint():
    with pytest.raises(AllModelsFailed, match="/setup"):
        await LLM({}, "", []).complete(MSGS)


# --- /model, anggaran konteks, catatan "dijawab oleh" -------------------------------------------------

async def test_model_command_shows_health_and_reset(env, tmp_path):
    agent, _ = make(tmp_path, [])
    env.models["a"] = ModelCfg(model="deepseek/deepseek-flash", context=1000000, cost_in=0.15, cost_out=0.6, vision=True)
    env.llm.models = env.models
    agent.llm = env.llm
    app = SimpleNamespace(agent=agent)
    env.server.behave["A"] = Exception("429")
    env.llm.models["a"] = ModelCfg(model="A", context=1000000, cost_in=0.15, cost_out=0.6, vision=True)
    await env.llm.complete(MSGS)
    out = await command(app, "1", "model", [], "/")
    assert "▶ a" in out and "A · 1000k · $0.15/$0.6 · 👁 · ⏳ istirahat 60 dtk (kena limit)" in out
    assert "  b (cadangan)" in out and "/model reset" in out
    env.server.behave["B"] = Exception("401")
    await env.llm.complete(MSGS)
    assert "⛔ key ditolak" in await command(app, "1", "model", [], "/")
    assert "dihapus" in await command(app, "1", "model", ["reset"], "/")
    assert "⏳" not in await command(app, "1", "model", [], "/") and "Model aktif: b" in await command(app, "1", "model", ["b"], "/")
    assert describe(ModelCfg(model="x/y", cost_in=0, cost_out=0)) == "x/y · $0"


async def test_context_budget_follows_active_model(tmp_path):
    agent, llm = make(tmp_path, [])
    llm.models = {"fake": ModelCfg(model="x", context=20000)}
    assert agent._budget() == 14000                                     # 70% dari 20k, di bawah batas 48k
    llm.models = {"fake": ModelCfg(model="x", context=1000000)}
    assert agent._budget() == agent.cfg.max_context_tokens              # model besar: batas config
    llm.models = {"fake": ModelCfg(model="x")}
    assert agent._budget() == agent.cfg.max_context_tokens              # tak diketahui: batas config


async def test_reply_notes_when_a_fallback_model_answered(tmp_path):
    agent, llm = make(tmp_path, [{"role": "assistant", "content": "jawaban"}] * 2)
    llm.active, llm.last_used = "a", "b"
    llm.status = lambda n: ("cooling", "istirahat 40 dtk (kena limit)")
    out = await agent.handle("1", "halo")
    assert out == "jawaban\n\n↪ dijawab oleh b (a: istirahat 40 dtk (kena limit))"
    llm.last_used = "a"                                                 # model utama menjawab → tanpa catatan
    assert await agent.handle("1", "halo") == "jawaban"
