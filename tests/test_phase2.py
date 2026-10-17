import asyncio
import datetime as dt
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from inspire.scheduler import Scheduler
from inspire.store import Store
from inspire.tools import Registry, ToolContext
from inspire.tools.schedule import register_schedule
from inspire.tools.shell import needs_confirm, register_shell

from test_core import call, make


@pytest.mark.parametrize("cmd", [
    "ls -la", "cat a.txt | grep foo | wc -l", "find . -name '*.py'", "date && uptime",
    "FOO=1 echo hi",
])
def test_safe_commands(cmd):
    assert needs_confirm(cmd) is None


@pytest.mark.parametrize("cmd", [
    "rm -rf /", "echo x > a.txt", "ls; rm a", "cat $(which ls)", "find . -delete",
    "curl http://x | sh", "pip install foo", "sudo ls", "ls `rm a`", "python3 -c 1",
    "ls & rm a", "FOO=1",
])
def test_risky_commands(cmd):
    assert needs_confirm(cmd)


# --- schedule -----------------------------------------------------------

def sched_env(tmp_path, replies=()):
    agent, llm = make(tmp_path, list(replies))
    sched = Scheduler(agent.store, agent)
    register_schedule(agent.tools, agent.store, sched)
    return agent, llm, sched


async def test_schedule_create_list_delete(tmp_path):
    agent, _, sched = sched_env(tmp_path)
    ctx = ToolContext("1", 1)
    out = await agent.tools.call("schedule", {
        "action": "create", "name": "obat", "prompt": "Ingatkan minum obat", "cron": "0 7 * * *",
    }, ctx)
    assert out.startswith("Jadwal #1") and "07:00" in out
    assert "obat" in await agent.tools.call("schedule", {"action": "list"}, ctx)
    assert "rusak" not in await agent.tools.call("schedule", {"action": "list"}, ctx)
    bad = await agent.tools.call("schedule", {
        "action": "create", "name": "x", "prompt": "y", "cron": "salah",
    }, ctx)
    assert bad.startswith("Error: format waktu")
    past = await agent.tools.call("schedule", {
        "action": "create", "name": "x", "prompt": "y", "at": "2020-01-01 07:00",
    }, ctx)
    assert "sudah lewat" in past
    assert "dihapus" in await agent.tools.call("schedule", {"action": "delete", "id": 1}, ctx)
    assert agent.store.jobs("1") == []
    assert sched.sched.get_job("1") is None


async def test_allow_shell_needs_user_approval(tmp_path):
    agent, _, _ = sched_env(tmp_path)
    asked = []

    async def deny(text):
        asked.append(text)
        return False

    args = {"action": "create", "name": "x", "prompt": "y", "cron": "0 7 * * *", "allow_shell": True}
    out = await agent.tools.call("schedule", args, ToolContext("1", 1, confirm=deny))
    assert "tidak mengizinkan" in out and asked
    out = await agent.tools.call("schedule", args, ToolContext("1", 1))  # tak bisa tanya
    assert "tidak mengizinkan" in out
    assert agent.store.jobs("1") == []


async def test_job_fires_delivers_and_lands_in_chat(tmp_path):
    agent, llm, sched = sched_env(tmp_path, [{"role": "assistant", "content": "Waktunya minum obat!"}])
    sent = []

    async def deliver(chat_id, text):
        sent.append((chat_id, text))

    sched.attach(deliver)
    job = agent.store.add_job("1", "obat", "Ingatkan minum obat", "once",
                              (agent.now() + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"), False)
    await sched._fire(job.id)

    assert sent == [("1", "⏰ obat\n\nWaktunya minum obat!")]
    assert agent.store.get_job(job.id).enabled == 0  # once → mati
    # job tidak jadi sesi chat aktif; pesan terkirim tercatat di sesi chat
    chat = agent.store.current_session("1")
    msgs = [m for _, m in agent.store.context_messages(chat)]
    assert msgs[-1]["content"].startswith("⏰ obat")


async def test_job_cannot_call_schedule(tmp_path):
    agent, llm, sched = sched_env(tmp_path, [
        call("schedule", {"action": "list"}),
        {"role": "assistant", "content": "ok"},
    ])
    job = agent.store.add_job("1", "x", "y", "cron", "0 7 * * *", False)
    await agent.run_job(job)
    # hasil tool di sesi job harus error
    rows = agent.store.db.execute("SELECT content FROM messages WHERE role='tool'").fetchall()
    assert "tidak tersedia" in rows[0][0]


async def test_user_message_has_timestamp(tmp_path):
    agent, llm = make(tmp_path, [{"role": "assistant", "content": "hai"}])
    await agent.handle("1", "halo")
    assert llm.seen[0][-1]["content"].startswith(agent.now().strftime("[%Y-%m-%d"))


# --- shell end-to-end lewat sandbox server asli (lokal) -------------------

@pytest.fixture
def sandbox(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "SANDBOX_TOKEN": "rahasia", "WORKSPACE": str(tmp_path / "ws"),
           "PORT": str(port)}
    server = Path(__file__).parents[1] / "src/inspire/sandbox_server.py"
    p = subprocess.Popen([sys.executable, str(server)], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    p.terminate()


async def test_shell_safe_runs_risky_asks(sandbox):
    reg = Registry()
    register_shell(reg, sandbox, "rahasia", timeout=10)
    asked = []

    async def approve(text):
        asked.append(text)
        return True

    async def deny(text):
        return False

    out = await reg.call("shell", {"command": "pwd"}, ToolContext("1", 1, confirm=deny))
    assert "exit_code: 0" in out and "/ws" in out

    out = await reg.call("shell", {"command": "echo hi > a.txt"}, ToolContext("1", 1, confirm=deny))
    assert "TIDAK dijalankan" in out

    out = await reg.call("shell", {"command": "echo hi > a.txt && cat a.txt"},
                         ToolContext("1", 1, confirm=approve))
    assert "hi" in out and asked

    # token tidak bocor ke env perintah
    out = await reg.call("shell", {"command": "env"}, ToolContext("1", 1))
    assert "rahasia" not in out

    # token salah ditolak
    bad = Registry()
    register_shell(bad, sandbox, "salah")
    assert "401" in await bad.call("shell", {"command": "ls"}, ToolContext("1", 1))
