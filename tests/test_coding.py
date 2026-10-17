import os

import pytest

from inspire.agent import STYLE
from inspire.tools import Registry, ToolContext
from inspire.tools.files import Outbox, Sandbox, register_files
from inspire.tools.shell import Grants, register_shell

from test_core import make
from test_phase2 import sandbox  # noqa: F401  (fixture server sandbox sungguhan)


@pytest.fixture
def tools(sandbox):  # noqa: F811
    reg, outbox, grants = Registry(), Outbox(), Grants()
    register_files(reg, Sandbox(sandbox, "rahasia"), outbox)
    register_shell(reg, sandbox, "rahasia", timeout=10, grants=grants)
    return reg, outbox, grants


async def test_write_read_edit_list(tools):
    reg, _, _ = tools
    ctx = ToolContext("1", 1)
    out = await reg.call("write_file", {"path": "proj/main.py", "content": "print('halo')\nx = 1\n"}, ctx)
    assert out.startswith("Dibuat: /workspace/proj/main.py")
    out = await reg.call("read_file", {"path": "/workspace/proj/main.py"}, ctx)
    assert "1│ print('halo')" in out and "dari 2" in out
    assert "1 tempat" in await reg.call("edit_file", {"path": "proj/main.py", "old": "x = 1", "new": "x = 2"}, ctx)
    assert "tidak ditemukan" in await reg.call("edit_file", {"path": "proj/main.py", "old": "zzz", "new": ""}, ctx)
    await reg.call("write_file", {"path": "proj/dua.txt", "content": "a\na\n"}, ctx)
    assert "muncul 2x" in await reg.call("edit_file", {"path": "proj/dua.txt", "old": "a", "new": "b"}, ctx)
    tree = await reg.call("list_files", {}, ctx)
    assert "proj/" in tree and "main.py" in tree
    # jalan tanpa tombol: tak ada confirm di ctx, tetap jalan
    run = await reg.call("shell", {"command": "cd proj && python3 main.py"}, ctx)
    assert "exit_code: 0" in run and "halo" in run


async def test_paths_cannot_escape_workspace(tools, tmp_path):
    reg, _, _ = tools
    ctx = ToolContext("1", 1)
    for path in ("../luar.txt", "/etc/passwd", "/workspace/../../x"):
        out = await reg.call("write_file", {"path": path, "content": "x"}, ctx)
        assert "di luar /workspace" in out, path
    ws = tmp_path / "ws"
    os.symlink("/etc", ws / "etc-link")
    assert "di luar /workspace" in await reg.call("read_file", {"path": "etc-link/hosts"}, ctx)


async def test_send_file(tools):
    reg, outbox, _ = tools
    ctx = ToolContext("42", 1)
    await reg.call("write_file", {"path": "hasil.txt", "content": "isi"}, ctx)
    assert "belum siap" in await reg.call("send_file", {"path": "hasil.txt"}, ctx)
    sent = []

    async def send(chat, name, data, caption):
        sent.append((chat, name, data, caption))

    outbox.send = send
    assert "Terkirim" in await reg.call("send_file", {"path": "hasil.txt", "caption": "ini"}, ctx)
    assert sent == [("42", "hasil.txt", b"isi", "ini")]


async def test_grant_30_minutes(tools):
    reg, _, grants = tools
    asked = []

    async def confirm(text, grant=False):
        asked.append(grant)
        grants.grant("1")  # user menekan "izinkan semua 30 menit"
        return True

    ctx = ToolContext("1", 1, confirm=confirm)
    await reg.call("shell", {"command": "rm -f a"}, ctx)
    await reg.call("shell", {"command": "rm -f b"}, ctx)
    assert asked == [True]                       # tanya sekali, lalu bebas
    assert grants.active("1") and not grants.active("2")

    # jadwal tak ikut izin, dan tak ditawari tombol izin
    job_asks = []

    async def job_confirm(text, **kw):
        job_asks.append(kw)
        return False

    out = await reg.call("shell", {"command": "rm -f c"}, ToolContext("1", 1, confirm=job_confirm, is_job=True))
    assert "TIDAK dijalankan" in out and job_asks == [{}]

    assert grants.revoke("1") and not grants.active("1")


async def test_caveman_modes(tmp_path):
    agent, llm = make(tmp_path, [{"role": "assistant", "content": "ok"}] * 3)
    for mode in ("auto", "on", "off"):
        agent.cfg.caveman = mode
        await agent.handle("1", "halo")
        system = llm.seen[-1][0]["content"]
        if mode == "off":
            assert "GAYA:" not in system
        else:
            assert STYLE[mode].splitlines()[0] in system


async def test_caveman_and_kunci_commands(tmp_path):
    from inspire import build
    from inspire.gateway.base import command
    from inspire.settings import Settings

    app = build(Settings(tmp_path))
    assert "otomatis" in await command(app, "1", "caveman", [], "/")
    assert "caveman" in (await command(app, "1", "caveman", ["on"], "/")).lower()
    assert app.cfg.caveman == "on" and Settings(tmp_path).get("style.caveman") == "on"
    assert "Pilih" in await command(app, "1", "caveman", ["x"], "/")
    assert "Tak ada izin" in await command(app, "1", "kunci", [], "/")
    app.grants.grant("1")
    assert "dicabut" in await command(app, "1", "kunci", [], "/")
