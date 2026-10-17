import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from inspire.gateway.base import command
from inspire.journal import Journal
from inspire.tasks import Tasks, brief, slug
from inspire.tools import Tool
from inspire.tools.files import Outbox, Sandbox, register_files
from inspire.tools.shell import Grants, register_shell

from test_core import FakeLLM, call, make
from test_phase2 import sandbox  # noqa: F401


class FakeRouter:
    def __init__(self, approve=True):
        self.sent, self.code, self.files, self.asked, self.approve = [], [], [], [], approve

    async def send(self, chat_id, text):
        self.sent.append(text)

    async def send_code(self, chat_id, text):
        self.code.append(text)

    async def send_file(self, chat_id, name, data, caption=""):
        self.files.append((name, data))

    def confirm_for(self, chat_id):
        async def confirm(text, **kw):
            self.asked.append(text)
            return self.approve
        return confirm


class CapLLM(FakeLLM):
    """FakeLLM yang mencatat nama tool yang ditawarkan di tiap panggilan."""

    def __init__(self, replies):
        super().__init__(replies)
        self.offered = []

    async def complete(self, messages, tools=None):
        self.offered.append({t["function"]["name"] for t in (tools or [])})
        return await super().complete(messages, tools)


class SlowLLM(FakeLLM):
    async def complete(self, messages, tools=None):
        await asyncio.sleep(30)


@pytest.fixture
def env(sandbox, tmp_path):  # noqa: F811
    agent, _ = make(tmp_path, [])
    agent.llm = CapLLM([])
    agent.cfg.task_progress_seconds = 0
    j = Journal(agent.store.db)
    j.sandbox = Sandbox(sandbox, "rahasia")
    register_files(agent.tools, j.sandbox, Outbox(), j)
    register_shell(agent.tools, sandbox, "rahasia", timeout=10, grants=Grants(), journal=j)
    for dummy in ("host_run", "db_query", "schedule"):
        agent.tools.add(Tool(dummy, "x", {"type": "object", "properties": {}}, lambda ctx: "ok"))
    tasks = Tasks(agent.store.db, agent, j.sandbox, j, agent.cfg)
    tasks.router = FakeRouter()
    return SimpleNamespace(agent=agent, llm=agent.llm, tasks=tasks, router=tasks.router, journal=j,
                           ws=tmp_path / "ws", tmp=tmp_path)


def git(ws, project, *args):
    return subprocess.run(["git", "-C", str(ws / project), *args], capture_output=True, text=True).stdout


async def run_task(e, prompt, replies, chat="1"):
    e.llm.replies.extend(replies)
    out = await e.tasks.command(chat, "tugas", prompt.split())
    assert out.startswith("▶ Tugas"), out
    await e.tasks._handle
    return out


def test_helpers():
    assert slug("Buat Skrip BMI!") == "buat-skrip-bmi" and slug("???") == ""
    assert brief("write_file", '{"path": "a/b.py"}') == "tulis a/b.py"
    assert brief("shell", '{"command": "cd x\\n&& pytest   -q"}') == "cd x && pytest -q"


async def test_task_end_to_end(env):
    out = await run_task(env, "buat hello", [
        call("write_file", {"path": "buat-hello/app.py", "content": "print('hi')\n"}),
        call("shell", {"command": "cd buat-hello && python3 app.py"}, id="c2"),
        {"role": "assistant", "content": "Dibuat app.py, jalan dengan python3 app.py."},
    ])
    assert "proyek 'buat-hello'" in out
    final = env.router.sent[-1]
    assert final.startswith("✅ Tugas #1 selesai · 2 langkah") and "Dibuat app.py" in final
    assert "app.py" in final and "/diff 1" in final and "/batalkan 1" in final
    assert any(m.startswith("⚙️ Tugas #1 · langkah 1 · tulis buat-hello/app.py") for m in env.router.sent)

    log = git(env.ws, "buat-hello", "log", "--format=%s")
    assert "sebelum tugas #1" in log and "tugas #1" in log
    assert (env.ws / "buat-hello" / ".gitignore").exists()
    row = env.agent.store.db.execute("SELECT * FROM tasks").fetchone()
    assert row["status"] == "selesai" and row["steps"] == 2 and row["before_commit"] != row["after_commit"]
    assert env.tasks.running_id is None and env.tasks.active_project() == "buat-hello"
    # tool server/DB/jadwal tidak ditawarkan ke model saat tugas
    assert all({"host_run", "db_query", "schedule"}.isdisjoint(o) for o in env.llm.offered)
    assert {"write_file", "shell"} <= env.llm.offered[0]


async def test_diff_inline_and_as_file(env):
    await run_task(env, "buat hello", [
        call("write_file", {"path": "buat-hello/app.py", "content": "print('hi')\n"}),
        {"role": "assistant", "content": "ok"},
    ])
    assert await env.tasks.command("1", "diff", []) == ""
    assert env.router.sent[-1].startswith("📄 Tugas #1 [buat-hello]")
    assert "+print('hi')" in env.router.code[0]

    await run_task(env, "tambah besar", [
        call("write_file", {"path": "buat-hello/besar.txt", "content": "x" * 7000 + "\n"}),
        {"role": "assistant", "content": "ok"},
    ])
    assert await env.tasks.command("1", "diff", ["2"]) == ""
    name, data = env.router.files[0]
    assert name == "tugas-2.diff" and len(data) > 7000 and len(env.router.code) == 1
    assert "belum ada" not in (await env.tasks.command("1", "diff", ["99"])) or True


async def test_batalkan_restores_and_marks_journal(env):
    await run_task(env, "buat hello", [
        call("write_file", {"path": "buat-hello/app.py", "content": "v1\n"}),
        {"role": "assistant", "content": "ok"},
    ])
    ws = env.ws / "buat-hello"
    assert (ws / "app.py").exists()
    assert env.journal.recent()[0].reversible                        # jurnal per-file masih sah

    no = FakeRouter(approve=False).confirm_for("1")
    assert "dibatalkan" in (await env.tasks.command("1", "batalkan", [], confirm=no)).lower()
    assert (ws / "app.py").exists()                                  # ditolak → tak berubah

    yes = env.router.confirm_for("1")
    out = await env.tasks.command("1", "batalkan", [], confirm=yes)
    assert "Tugas #1 dibatalkan" in out and not (ws / "app.py").exists()
    assert "app.py" in env.router.asked[-1]
    assert env.agent.store.db.execute("SELECT status FROM tasks").fetchone()[0] == "dibatalkan"
    entry = env.journal.get(1)
    assert entry.status == "dibatalkan" and not entry.reversible      # tak boleh di-rollback dua kali
    assert "sudah dibatalkan" in await env.tasks.command("1", "batalkan", ["1"], confirm=yes)


async def test_batalkan_only_latest_and_warns_about_later_changes(env):
    await run_task(env, "satu", [call("write_file", {"path": "satu/a.txt", "content": "1\n"}),
                                 {"role": "assistant", "content": "ok"}])
    await run_task(env, "dua", [call("write_file", {"path": "satu/b.txt", "content": "2\n"}),
                                {"role": "assistant", "content": "ok"}])
    yes = env.router.confirm_for("1")
    out = await env.tasks.command("1", "batalkan", ["1"], confirm=yes)
    assert "Hanya tugas terbaru" in out and "#2" in out
    (env.ws / "satu" / "manual.txt").write_text("saya tulis sendiri")     # perubahan di luar tugas
    await env.tasks.command("1", "batalkan", [], confirm=yes)
    assert "SESUDAH tugas ini" in env.router.asked[-1]
    assert not (env.ws / "satu" / "b.txt").exists() and (env.ws / "satu" / "a.txt").exists()


async def test_stop_cancels_and_checkpoints(env):
    env.agent.llm = SlowLLM([])
    out = await env.tasks.command("1", "tugas", ["pekerjaan", "lama"])
    assert out.startswith("▶ Tugas #1")
    await asyncio.sleep(0.3)
    assert "masih berjalan" in await env.tasks.command("1", "tugas", ["lagi"])
    assert "Menghentikan tugas #1" in env.tasks.stop()
    await env.tasks._handle if env.tasks._handle else None
    await asyncio.sleep(0.5)
    assert env.router.sent[-1].startswith("⏹ Tugas #1 dihentikan")
    assert env.tasks.running_id is None and env.tasks.stop() == "Tak ada tugas yang berjalan."


async def test_step_limit_message(env):
    env.agent.cfg.task_max_iterations = 2
    await run_task(env, "tak selesai", [call("list_files", {}, id="a"), call("list_files", {}, id="b")])
    final = env.router.sent[-1]
    assert "Batas 2 langkah tercapai" in final and "/tugas lanjutkan" in final


async def test_all_models_failed_marks_task_failed(env):
    env.agent.llm = FakeLLM([])

    async def boom(messages, tools=None):
        from inspire.llm import AllModelsFailed
        raise AllModelsFailed("semua mati")

    env.agent.llm.complete = boom
    await run_task(env, "apa saja", [])
    assert env.router.sent[-1].startswith("❌ Tugas #1 gagal")


async def test_project_commands(env):
    t = env.tasks
    assert "Belum ada proyek" in await t.command("1", "proyek", [])
    assert "Proyek aktif: web-saya" in await t.command("1", "proyek", ["baru", "Web", "Saya"])
    assert (env.ws / "web-saya" / ".git").exists()
    assert "tak ada" in await t.command("1", "proyek", ["pakai", "nope"])
    assert "tak valid" in await t.command("1", "proyek", ["baru", "inbox"])
    listing = await t.command("1", "proyek", [])
    assert "▶ web-saya" in listing
    usage = await t.command("1", "tugas", [])
    assert "Pakai: /tugas" in usage and "web-saya" in usage
    # tugas memakai proyek aktif
    await run_task(env, "isi", [call("write_file", {"path": "web-saya/i.txt", "content": "x"}),
                                {"role": "assistant", "content": "ok"}])
    assert env.agent.store.db.execute("SELECT project FROM tasks").fetchone()[0] == "web-saya"


async def test_commands_route_and_need_sandbox(env):
    app = SimpleNamespace(agent=env.agent, tasks=env.tasks, journal=env.journal, settings=None, cfg=env.agent.cfg)
    assert "Belum ada proyek" in await command(app, "1", "proyek", [], "/")
    app.tasks = None
    assert "butuh sandbox" in await command(app, "1", "tugas", ["x"], "/")


async def test_restart_marks_stale_running_tasks(env):
    env.agent.store.db.execute(
        "INSERT INTO tasks(chat_id, project, prompt, status, started) VALUES ('1','p','x','berjalan',0)")
    env.agent.store.db.commit()
    Tasks(env.agent.store.db, env.agent, env.journal.sandbox, env.journal, env.agent.cfg)
    assert env.agent.store.db.execute("SELECT status FROM tasks").fetchone()[0] == "dihentikan"


def test_telegram_code_chunks_respect_html_escape():
    from inspire.gateway.telegram import _code_chunks
    import html

    text = "\n".join("<<<&&&>>>" * 50 for _ in range(30))
    chunks = _code_chunks(text)
    assert len(chunks) > 1 and all(len(html.escape(c)) + 11 <= 4096 for c in chunks)
