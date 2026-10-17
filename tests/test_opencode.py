import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from inspire import opencode as oc
from inspire.gateway.base import command
from inspire.journal import Journal
from inspire.tasks import Tasks
from inspire.tools.files import Sandbox

from test_core import make
from test_phase2 import sandbox  # noqa: F401
from test_tasks import FakeRouter

FIX = Path(__file__).parent / "fixtures" / "opencode"


def fixture(name):
    return (FIX / f"{name}.jsonl").read_text().splitlines()


# --- parser (keluaran OpenCode asli) ---------------------------------------------------------------

def summarize(name):
    s, briefs = oc.Summary(), []
    for line in fixture(name):
        b = s.feed(line)
        if b:
            briefs.append(b)
    return s, briefs


def test_parse_real_success_with_tools():
    s, briefs = summarize("sukses_tool")
    assert s.session.startswith("ses_") and s.steps == 2 and s.tools == {"write": 1, "bash": 1}
    assert briefs == ["tulis proj/hello.py", "python3 hello.py"]
    assert s.final_text == "hi" and s.error is None and s.failed_tools == 0
    assert s.tokens_in > 0 and s.tokens_out > 0


def test_parse_text_only_and_denied_command():
    s, _ = summarize("sukses_teks")
    assert s.steps == 0 and s.final_text == "Hi"
    s, briefs = summarize("izin_ditolak")
    assert s.steps == 3 and s.failed_tools == 1 and briefs[1].endswith("✗")      # rm yang di-deny ditandai gagal
    assert "Blocked" in s.final_text


def test_parse_errors_and_explanations():
    s, _ = summarize("error_key")
    assert s.error == {"name": "APIError", "message": "Upstream request failed: Invalid credential", "status": 401}
    assert "Key ditolak" in oc.explain_error(s.error)
    s, _ = summarize("error_model")
    assert s.error["name"] == "UnknownError" and "/opencode models" in oc.explain_error(s.error)


def test_parser_ignores_garbage_and_final_text_is_after_last_tool():
    s = oc.Summary()
    for line in ["", "bukan json", "[1,2]", '{"type":"text","part":{"text":"awal"}}',
                 '{"type":"tool_use","part":{"tool":"read","state":{"input":{"filePath":"/a/b/c.py"}}}}',
                 '{"type":"text","part":{"text":"akhir"}}']:
        s.feed(line)
    assert s.final_text == "akhir" and s.steps == 1
    t = oc.Summary()
    for line in ['{"type":"text","part":{"text":"hanya ini"}}',
                 '{"type":"tool_use","part":{"tool":"bash","state":{"input":{"command":"ls"}}}}']:
        t.feed(line)
    assert t.final_text == "hanya ini"                                               # tak ada teks setelah tool terakhir


def test_brief_tool_variants():
    assert oc.brief_tool("edit", {"input": {"filePath": "/w/p/src/app.py"}}) == "ubah src/app.py"
    assert oc.brief_tool("read", {"input": {"filePath": "/w/p/x.py"}, "status": "error"}) == "baca p/x.py ✗"
    assert oc.brief_tool("grep", {"input": {"pattern": "TODO"}}) == "cari TODO"
    assert oc.brief_tool("webfetch", {"input": {"url": "https://x.dev"}}) == "ambil https://x.dev"
    assert oc.brief_tool("todowrite", {}) == "todowrite"


def test_parse_models():
    assert oc.parse_models("opencode/big-pickle\nopencode/glm-5.3-flash-free\n\nwarning: sesuatu\n") == [
        "opencode/big-pickle", "opencode/glm-5.3-flash-free"]


def test_argv_env_prompt():
    argv = oc.build_argv("opencode", "kerjakan x")
    assert argv[:5] == ["opencode", "run", "--pure", "--format", "json"] and argv[-1] == "kerjakan x"
    assert "--auto" not in argv and "-m" not in argv and "-s" not in argv           # tanpa --auto: izin 'ask' ditolak otomatis
    argv = oc.build_argv("opencode", "p", model="opencode/big-pickle", session="ses_1")
    assert argv[5:9] == ["-m", "opencode/big-pickle", "-s", "ses_1"]
    env = oc.build_env()
    assert "OPENCODE_API_KEY" not in env and env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    perm = json.loads(env["OPENCODE_PERMISSION"])
    assert perm["external_directory"] == "deny" and perm["bash"]["*"] == "allow"
    assert all(perm["bash"][k] == "deny" for k in ("rm -rf *", "sudo *", "git push *"))
    assert list(perm["bash"])[0] == "*"                                              # catch-all dulu: aturan terakhir menang
    assert oc.build_env("sk-zen")["OPENCODE_API_KEY"] == "sk-zen"
    p = oc.build_prompt("  buat api  ")
    assert p.startswith("Kamu bekerja di folder proyek") and p.endswith("TUGAS:\nbuat api")


# --- OpenCode palsu: memutar ulang event asli, dan mencatat apa yang diterimanya ---------------------

FAKE = '''#!{py}
import json, os, pathlib, sys, time
FIX = pathlib.Path({fix!r})
args = sys.argv[1:]
cwd = os.getcwd()
if args[:1] == ["--version"]:
    print("1.19.3-fake"); sys.exit(0)
if args[:1] == ["models"]:
    print("opencode/big-pickle"); print("opencode/glm-5.3-flash-free")
    if os.environ.get("OPENCODE_API_KEY"):
        print("opencode/claude-haiku-4-5")
    sys.exit(0)
prompt = args[-1]
log = {{"argv": args, "cwd": cwd, "stdin": sys.stdin.read(), "perm": os.environ.get("OPENCODE_PERMISSION"),
       "key": os.environ.get("OPENCODE_API_KEY"), "pid": os.getpid()}}
with open(pathlib.Path(cwd).parent / ".fake_oc.json", "a") as f:
    f.write(json.dumps(log) + "\\n")

def replay(name, delay=0.0):
    for line in (FIX / (name + ".jsonl")).read_text().splitlines():
        print(line.replace("/workspace/proj", cwd), flush=True)
        time.sleep(delay)

if "[[sukses]]" in prompt:
    pathlib.Path(cwd, "hello.py").write_text('print("hi")\\n')
    replay("sukses_tool", 0.05)
elif "[[key]]" in prompt:
    replay("error_key"); sys.exit(1)
elif "[[model]]" in prompt:
    replay("error_model"); sys.exit(1)
elif "[[lambat]]" in prompt:
    for i in range(400):
        print(json.dumps({{"type": "tool_use", "sessionID": "ses_lambat", "part": {{"type": "tool", "tool": "bash",
              "state": {{"status": "completed", "input": {{"command": f"langkah {{i}}"}}}}}}}}), flush=True)
        time.sleep(0.1)
elif "[[banyak]]" in prompt:
    for i in range(400):
        print(json.dumps({{"type": "tool_use", "sessionID": "ses_banyak", "part": {{"type": "tool", "tool": "bash",
              "state": {{"status": "completed", "input": {{"command": f"langkah {{i}}"}}}}}}}}), flush=True)
        time.sleep(0.02)
elif "[[bisu]]" in prompt:
    print(json.dumps({{"type": "tool_use", "sessionID": "ses_bisu", "part": {{"type": "tool", "tool": "bash",
          "state": {{"status": "completed", "input": {{"command": "ls"}}}}}}}}), flush=True)
elif "[[crash]]" in prompt:
    print("kaboom", file=sys.stderr); sys.exit(7)
else:
    replay("sukses_teks")
'''


@pytest.fixture
def env(sandbox, tmp_path):  # noqa: F811
    fake = tmp_path / "fake-opencode"
    fake.write_text(FAKE.format(py=sys.executable, fix=str(FIX)))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    agent, _ = make(tmp_path, [])
    cfg = agent.cfg
    cfg.opencode_bin, cfg.opencode_poll, cfg.task_progress_seconds = str(fake), 0.05, 0
    j = Journal(agent.store.db)
    j.sandbox = Sandbox(sandbox, "rahasia")
    tasks = Tasks(agent.store.db, agent, j.sandbox, j, cfg)
    tasks.router = FakeRouter()
    ws = tmp_path / "ws"
    return SimpleNamespace(agent=agent, cfg=cfg, tasks=tasks, router=tasks.router, journal=j, ws=ws,
                           log=lambda: [json.loads(x) for x in (ws / ".fake_oc.json").read_text().splitlines()])


async def oc_task(e, text, args_prefix=()):
    out = await e.tasks.command("1", "opencode", [*args_prefix, text])
    assert out.startswith("▶ Tugas"), out
    await e.tasks._handle
    return out


async def test_status_models_and_model_selection(env):
    e = env
    info = await e.tasks.command("1", "opencode", [])
    assert "OpenCode 1.19.3-fake" in info and "otomatis" in info and "model gratis bawaan" in info
    models = await e.tasks.command("1", "opencode", ["models"])
    assert "opencode/big-pickle" in models and "claude-haiku" not in models and "hanya model gratis" in models
    assert "tak ada di daftar" in await e.tasks.command("1", "opencode", ["model", "big-pikle"])
    assert "Mirip: opencode/big-pickle" in await e.tasks.command("1", "opencode", ["model", "pickle"])
    assert "Model OpenCode: opencode/big-pickle" == await e.tasks.command("1", "opencode", ["model", "opencode/big-pickle"])
    assert e.tasks.oc_model() == "opencode/big-pickle"
    assert "▶ opencode/big-pickle" in await e.tasks.command("1", "opencode", ["models"])
    assert "otomatis" in await e.tasks.command("1", "opencode", ["model", "auto"]) and e.tasks.oc_model() == ""


async def test_paid_models_only_listed_with_a_dedicated_key(env):
    env.cfg.opencode_key = "sk-zen"
    models = await env.tasks.command("1", "opencode", ["models"])
    assert "opencode/claude-haiku-4-5" in models and "sk-zen" not in models
    assert "ada (model berbayar)" in await env.tasks.command("1", "opencode", []) and "sk-zen" not in await env.tasks.command("1", "opencode", [])


async def test_not_installed_gives_update_hint(env):
    env.cfg.opencode_bin = "opencode-tidak-ada"
    out = await env.tasks.command("1", "opencode", ["buat sesuatu"])
    assert "belum bisa dipakai" in out and "inspire update" in out and env.tasks.running_id is None


async def test_full_task_end_to_end(env):
    e = env
    await e.tasks.command("1", "opencode", ["model", "opencode/big-pickle"])
    out = await oc_task(e, "buat hello [[sukses]]")
    assert "(OpenCode · opencode/big-pickle)" in out and "proyek 'buat-hello-sukses'" in out

    final = e.router.sent[-1]
    assert final.startswith("✅ Tugas #1 selesai · 2 langkah") and "🧰 OpenCode · opencode/big-pickle" in final
    assert "token" in final and "hello.py" in final and "/diff 1" in final
    assert any(m.startswith("⚙️ Tugas #1 · langkah 1 · tulis ") for m in e.router.sent)       # kabar kemajuan nyata
    assert (e.ws / "buat-hello-sukses" / "hello.py").read_text() == 'print("hi")\n'

    row = e.agent.store.db.execute("SELECT * FROM tasks").fetchone()
    assert row["engine"] == "opencode" and row["status"] == "selesai" and row["session"].startswith("ses_")
    assert row["tokens"] > 0 and row["model"] == "opencode/big-pickle" and row["after_commit"] != row["before_commit"]

    # apa yang diterima OpenCode: stdin ditutup, cwd = proyek, tanpa --auto, izin terkunci, tanpa key
    seen = e.log()[-1]
    assert seen["stdin"] == "" and seen["cwd"].endswith("/ws/buat-hello-sukses") and seen["key"] is None
    assert seen["argv"][:5] == ["run", "--pure", "--format", "json", "-m"] and "--auto" not in seen["argv"]
    assert json.loads(seen["perm"])["bash"]["sudo *"] == "deny"
    assert "buat hello" in seen["argv"][-1] and seen["argv"][-1].startswith("Kamu bekerja di folder proyek")

    # sama seperti mode tugas biasa: /diff dan /batalkan
    assert await e.tasks.command("1", "diff", []) == ""
    assert "+print(\"hi\")" in e.router.code[0]
    assert "Tugas #1 dibatalkan" in await e.tasks.command("1", "batalkan", [], confirm=e.router.confirm_for("1"))
    assert not (e.ws / "buat-hello-sukses" / "hello.py").exists()
    assert any("OpenCode · tugas #1" in x.line() for x in e.journal.recent())                              # jejak audit di jurnal


async def test_continue_uses_the_stored_session_not_global_continue(env):
    e = env
    assert "Belum ada sesi OpenCode" in (await e.tasks.command("1", "proyek", ["baru", "app"]) and
                                          await e.tasks.command("1", "opencode", ["lanjut", "tambah tes"]))
    await oc_task(e, "buat [[sukses]]")
    sid = e.agent.store.db.execute("SELECT session FROM tasks").fetchone()[0]
    out = await e.tasks.command("1", "opencode", ["lanjut", "tambah", "tes", "[[sukses]]"])
    assert out.startswith("▶ Tugas #2")
    await e.tasks._handle
    argv = e.log()[-1]["argv"]
    assert argv[argv.index("-s") + 1] == sid and "-c" not in argv and "--continue" not in argv
    assert "Pakai: /opencode lanjut" in await e.tasks.command("1", "opencode", ["lanjut"])


async def test_key_is_injected_only_when_configured(env):
    await oc_task(env, "x [[sukses]]")
    assert env.log()[-1]["key"] is None
    env.cfg.opencode_key = "sk-zen"
    await oc_task(env, "y [[sukses]]")
    assert env.log()[-1]["key"] == "sk-zen"
    assert "sk-zen" not in " ".join(env.router.sent)                                            # tak pernah dikirim ke chat


async def test_api_errors_become_helpful_failures(env):
    await oc_task(env, "x [[key]]")
    final = env.router.sent[-1]
    assert final.startswith("❌ Tugas #1 gagal") and "Key ditolak" in final
    await oc_task(env, "y [[model]]")
    assert "/opencode models" in env.router.sent[-1]
    assert env.agent.store.db.execute("SELECT status FROM tasks ORDER BY id DESC").fetchone()[0] == "gagal"


async def test_crash_and_silent_runs(env):
    await oc_task(env, "x [[crash]]")
    assert env.router.sent[-1].startswith("❌ Tugas #1 gagal") and "kode 7" in env.router.sent[-1] and "kaboom" in env.router.sent[-1]
    await oc_task(env, "y [[bisu]]")
    assert env.router.sent[-1].startswith("✅ Tugas #2 selesai") and "tak memberi ringkasan" in env.router.sent[-1]


async def test_stop_kills_the_process_in_the_sandbox(env):
    e = env
    assert (await e.tasks.command("1", "opencode", ["kerja lama [[lambat]]"])).startswith("▶ Tugas #1")
    await asyncio.sleep(0.8)
    assert "masih berjalan" in await e.tasks.command("1", "opencode", ["lagi"])
    pid = e.log()[-1]["pid"]
    os.kill(pid, 0)                                                                               # masih hidup
    assert "Menghentikan tugas #1" in e.tasks.stop()
    await e.tasks._handle if e.tasks._handle else None
    await asyncio.sleep(0.5)
    assert e.router.sent[-1].startswith("⏹ Tugas #1 dihentikan")
    with pytest.raises(ProcessLookupError):
        for _ in range(40):                                                                       # beri waktu SIGTERM bekerja
            os.kill(pid, 0)
            await asyncio.sleep(0.1)
    assert e.tasks.running_id is None


async def test_step_limit_kills_and_reports(env):
    env.cfg.opencode_max_steps = 5
    await oc_task(env, "x [[banyak]]")
    final = env.router.sent[-1]
    assert final.startswith("⏹ Tugas #1 dihentikan") and "Melewati batas 5 langkah" in final and "/opencode lanjut" in final
    await asyncio.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        for _ in range(30):
            os.kill(env.log()[-1]["pid"], 0)
            await asyncio.sleep(0.1)


async def test_time_limit_stops_the_job(env):
    env.cfg.task_max_minutes = 0.03                                                               # ±1,8 detik
    await oc_task(env, "x [[lambat]]")
    assert env.router.sent[-1].startswith("⏹ Tugas #1 dihentikan") and "Waktu" in env.router.sent[-1]


async def test_commands_route_and_help_lists_opencode(env):
    app = SimpleNamespace(agent=env.agent, tasks=env.tasks, journal=env.journal, settings=None, cfg=env.cfg)
    assert "OpenCode 1.19.3-fake" in await command(app, "1", "opencode", [], "/")
    assert "/opencode <tugas>" in await command(app, "1", "help", [], "/")
    app.tasks = None
    assert "butuh sandbox" in await command(app, "1", "opencode", ["x"], "/")


def test_old_task_table_is_migrated(tmp_path):
    import sqlite3

    db = sqlite3.connect(tmp_path / "old.db")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, chat_id TEXT, project TEXT, prompt TEXT, status TEXT, "
               "before_commit TEXT, after_commit TEXT, started REAL, ended REAL, steps INTEGER, journal_from INTEGER, "
               "journal_to INTEGER, summary TEXT)")
    db.execute("INSERT INTO tasks(chat_id, project, prompt, status, started) VALUES ('1','p','x','selesai',0)")
    db.execute("CREATE TABLE journal (id INTEGER PRIMARY KEY)")
    agent, _ = make(tmp_path, [])
    Tasks(db, agent, None, None, agent.cfg)
    row = db.execute("SELECT * FROM tasks").fetchone()
    assert row["engine"] == "inspire" and row["session"] is None and row["tokens"] == 0


# --- setup: status dan key opsional -------------------------------------------------------------------

async def test_setup_status_and_dedicated_key(sandbox, tmp_path, monkeypatch):  # noqa: F811
    from inspire import build
    from inspire import setup as S
    from inspire.gateway.tg_setup import TelegramSetup
    from inspire.settings import Settings

    fake = tmp_path / "fake-opencode"
    fake.write_text(FAKE.format(py=sys.executable, fix=str(FIX)))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    for k in ("SANDBOX_URL", "SANDBOX_TOKEN", "OPENCODE_SANDBOX_KEY"):
        monkeypatch.delenv(k, raising=False)

    s = Settings(tmp_path / "st")
    row = {f.id: f for f in await S.status(s)}["opencode"]
    assert row.state == "off" and "Docker" in row.detail                                    # tanpa sandbox

    monkeypatch.setenv("SANDBOX_URL", sandbox)
    monkeypatch.setenv("SANDBOX_TOKEN", "rahasia")
    row = {f.id: f for f in await S.status(s, live=True)}["opencode"]
    assert row.state == "err" and "inspire update" in row.fix                              # biner 'opencode' tak ada di sandbox
    s.set("opencode.bin", str(fake))
    row = {f.id: f for f in await S.status(s, live=True)}["opencode"]
    assert row.state == "on" and "1.19.3-fake" in row.detail and "model gratis bawaan" in row.detail

    # key khusus lewat /setup Telegram: tersimpan terpisah, langsung aktif, tak pernah tampil di chat
    app = build(s)
    assert app.cfg.opencode_key == "" and "opencode" in S.KEY_SECTIONS and "KHUSUS" in S.KEY_SECTIONS["opencode"].why
    setup = TelegramSetup(app)
    sent = []

    async def send_message(text, reply_markup=None, **kw):
        sent.append(text)

    async def noop(*a, **k):
        pass

    def upd(text="", data=None):
        q = SimpleNamespace(data=data, answer=noop, edit_message_text=lambda t, reply_markup=None, **kw: noop()) if data else None
        return SimpleNamespace(effective_chat=SimpleNamespace(id=1, send_message=send_message),
                               effective_message=SimpleNamespace(text=text, delete=noop), callback_query=q)

    await setup.on_button(upd(data="su:key:opencode"))
    await setup.on_text(upd(text="sk-zen-khusus"))
    assert s.key("OPENCODE_SANDBOX_KEY") == "sk-zen-khusus" and "OPENCODE_API_KEY" not in s.data["keys"]
    app.reload()
    assert app.cfg.opencode_key == "sk-zen-khusus"
    assert "sk-zen-khusus" not in " ".join(sent)
    row = {f.id: f for f in await S.status(s)}["opencode"]
    assert "key berbayar terpasang" in row.detail


async def test_oom_kill_gets_a_memory_hint(env):
    e = env
    out = oc.Outcome(oc.Summary(), -9)
    status, reply, _ = e.tasks._oc_result(out, "")
    assert status == "gagal" and "kehabisan memori" in reply and "SANDBOX_MEM" in reply
