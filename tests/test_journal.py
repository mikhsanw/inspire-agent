from types import SimpleNamespace

import pytest

from inspire.gateway.base import command
from inspire.journal import Journal
from inspire.store import Store
from inspire.tools import Registry, ToolContext
from inspire.tools.files import Outbox, Sandbox, register_files
from inspire.tools.host import Sites, register_host
from inspire.tools.db import register_db
from inspire.tools.shell import Grants, is_readonly, register_shell

from test_phase2 import sandbox  # noqa: F401
from test_host import LocalHost, site as host_site, confirmer  # noqa: F401


def make_app(journal):
    return SimpleNamespace(journal=journal, agent=None)


@pytest.fixture
def sb_env(sandbox, tmp_path):  # noqa: F811
    j = Journal(Store(tmp_path / "j.db").db)
    reg = Registry()
    j.sandbox = Sandbox(sandbox, "rahasia")
    register_files(reg, j.sandbox, Outbox(), j)
    register_shell(reg, sandbox, "rahasia", timeout=10, grants=Grants(), journal=j)
    return reg, j, tmp_path / "ws"


def test_is_readonly():
    assert is_readonly("ls -la && cat a | grep b 2>&1")
    assert not is_readonly("python x.py") and not is_readonly("echo a > b") and not is_readonly("find . -delete")


async def test_sandbox_write_edit_and_rollback(sb_env):
    reg, j, ws = sb_env
    ctx = ToolContext("1", 1)
    out = await reg.call("write_file", {"path": "app.py", "content": "v1\n"}, ctx)
    assert "[jurnal #1]" in out
    await reg.call("edit_file", {"path": "app.py", "old": "v1", "new": "v2"}, ctx)
    await reg.call("write_file", {"path": "app.py", "content": "v3\n"}, ctx)
    assert (ws / "app.py").read_text() == "v3\n"

    yes, asked = confirmer(True)
    app = make_app(j)
    out = await command(app, "1", "rollback", ["3"], "/", confirm=yes)
    assert "#3 dibatalkan" in out and (ws / "app.py").read_text() == "v2\n"
    assert "dikembalikan ke isi sebelum #3" in asked[0][0]
    await command(app, "1", "rollback", ["2"], "/", confirm=yes)
    assert (ws / "app.py").read_text() == "v1\n"
    await command(app, "1", "rollback", ["1"], "/", confirm=yes)
    assert not (ws / "app.py").exists()                    # file baru → dihapus

    assert "sudah dibatalkan" in await command(app, "1", "rollback", ["1"], "/", confirm=yes)
    hist = await command(app, "1", "riwayat", [], "/")
    assert "↩️ dibatalkan" in hist and "batalkan #3" in hist


async def test_shell_logged_but_not_reversible(sb_env):
    reg, j, _ = sb_env
    ctx = ToolContext("1", 1)
    await reg.call("shell", {"command": "ls"}, ctx)               # baca → tidak dicatat
    out = await reg.call("shell", {"command": "mkdir -p proj"}, ctx)
    assert "jurnal #1" in out and len(j.recent()) == 1
    no_undo = await command(make_app(j), "1", "rollback", ["1"], "/", confirm=confirmer(True)[0])
    assert "tak bisa dibatalkan otomatis" in no_undo


async def test_rollback_needs_button(sb_env):
    reg, j, ws = sb_env
    await reg.call("write_file", {"path": "a.txt", "content": "x"}, ToolContext("1", 1))
    no, _ = confirmer(False)
    assert "dibatalkan." in (await command(make_app(j), "1", "rollback", ["1"], "/", confirm=no)).lower()
    assert (ws / "a.txt").exists()
    assert "Pakai:" in await command(make_app(j), "1", "rollback", [], "/")


async def test_server_actions_journaled_and_rolled_back(host_site, tmp_path):  # noqa: F811
    _, root, host = host_site
    j = Journal(Store(tmp_path / "j2.db").db)
    sites = Sites(host, [{"name": "izin", "path": str(root), "owner": "me"}], j)
    j.sites = sites
    reg = Registry()
    register_host(reg, sites)
    register_db(reg, sites)
    yes, _ = confirmer(True)
    ctx = ToolContext("1", 1, confirm=yes)

    await reg.call("host_edit", {"site": "izin", "path": "app/Ctrl.php", "old": "'lama'", "new": "'baru'"}, ctx)
    out = await reg.call("host_run", {"site": "izin", "command": "touch storage/x.txt"}, ctx)
    assert "/rollback 2" in out
    await reg.call("db_query", {"site": "izin", "sql": "SELECT id FROM pemohon"}, ctx)
    lines = j.render().splitlines()
    assert "db:izin" in lines[0] and "⏪" not in lines[0]       # audit, tak bisa dibatalkan
    assert "ubah app/Ctrl.php" in lines[2] and "⏪ bisa" in lines[2]

    out = await command(make_app(j), "1", "rollback", ["1"], "/", confirm=yes)
    assert "#1 dibatalkan" in out
    assert (root / "app/Ctrl.php").read_text() == "<?php\nreturn 'lama';\n"


async def test_agent_can_read_journal(tmp_path):
    from inspire import build
    from inspire.settings import Settings

    app = build(Settings(tmp_path))
    app.journal.add("1", "sandbox", "shell", "pip install requests")
    out = await app.agent.tools.call("riwayat_kerja", {}, ToolContext("1", 1))
    assert "pip install requests" in out
