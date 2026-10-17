"""Job latar belakang di sandbox_server (server sungguhan, proses sungguhan)."""

import asyncio
import os
import sys
import time

import pytest

from inspire.tools.files import Sandbox

from test_phase2 import sandbox  # noqa: F401


@pytest.fixture
def sb(sandbox, tmp_path):  # noqa: F811
    return Sandbox(sandbox, "rahasia"), tmp_path / "ws"


async def poll(sb, jid, until_done=True, timeout=15):
    """Kumpulkan keluaran sampai job selesai. → (teks, hasil terakhir)."""
    out, offset, end = "", 0, time.time() + timeout
    while time.time() < end:
        r = await sb.call("job/read", id=jid, offset=offset)
        out += r["data"]
        offset = r["next"]
        if not r["running"]:
            return out, r
        await asyncio.sleep(0.05)
    raise AssertionError("job tak selesai")


async def test_job_streams_lines_and_reports_exit(sb):
    sbx, ws = sb
    code = "import sys,time\nfor i in range(3):\n    print('baris', i, flush=True); time.sleep(0.15)\nsys.exit(3)"
    jid = (await sbx.call("job/start", argv=[sys.executable, "-c", code]))["id"]
    first = await sbx.call("job/read", id=jid, offset=0)
    assert first["running"] and first["exit_code"] is None                 # belum selesai: hanya baris utuh
    assert first["data"] == "" or first["data"].endswith("\n")
    out, last = await poll(sbx, jid)
    assert "baris 0" in first["data"] + out and out.count("baris") + first["data"].count("baris") == 3
    assert last["exit_code"] == 3 and last["timed_out"] is False


async def test_job_offset_never_repeats_or_loses_lines(sb):
    sbx, _ = sb
    code = "import time\nfor i in range(20):\n    print(i, flush=True); time.sleep(0.02)"
    jid = (await sbx.call("job/start", argv=[sys.executable, "-c", code]))["id"]
    out, _ = await poll(sbx, jid)
    assert out.split() == [str(i) for i in range(20)]
    again = await sbx.call("job/read", id=jid, offset=len(out))
    assert again["data"] == "" and again["next"] == len(out)


async def test_job_cwd_env_and_stdin_closed(sb):
    sbx, ws = sb
    (ws / "proj").mkdir()
    code = ("import os,sys\nprint(os.getcwd())\nprint(os.environ.get('SAYA'), os.environ.get('SANDBOX_TOKEN'))\n"
            "print(repr(sys.stdin.read()))")                                # stdin harus langsung EOF
    jid = (await sbx.call("job/start", argv=[sys.executable, "-c", code], cwd="proj", env={"SAYA": "halo"}))["id"]
    out, last = await poll(sbx, jid)
    lines = out.splitlines()
    assert lines[0].endswith("/ws/proj") and lines[1] == "halo None" and lines[2] == "''"
    assert last["exit_code"] == 0


async def test_job_timeout_and_kill_stop_whole_process_group(sb):
    sbx, ws = sb
    sleeper = "import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\ntime.sleep(60)"
    jid = (await sbx.call("job/start", argv=[sys.executable, "-c", sleeper], timeout=1))["id"]
    out, last = await poll(sbx, jid)
    assert last["timed_out"] is True and last["exit_code"] != 0

    jid = (await sbx.call("job/start", argv=[sys.executable, "-c", "import time;time.sleep(60)"]))["id"]
    await asyncio.sleep(0.2)
    assert (await sbx.call("job/read", id=jid))["running"]
    await sbx.call("job/kill", id=jid)
    out, last = await poll(sbx, jid)
    assert last["exit_code"] is not None and last["exit_code"] != 0


async def test_job_stderr_tail_and_bad_requests(sb):
    sbx, _ = sb
    jid = (await sbx.call("job/start", argv=[sys.executable, "-c", "import sys;print('rusak', file=sys.stderr);sys.exit(1)"]))["id"]
    _, last = await poll(sbx, jid)
    assert last["exit_code"] == 1 and "rusak" in last["err"]
    for bad in ({"argv": []}, {"argv": "ls"}, {"argv": ["x"], "cwd": "/etc"}, {"argv": ["tidak-ada-perintah-ini"]},
                {"argv": ["ls"], "env": {"A": 1}}, {"argv": ["ls"], "cwd": "tidak/ada"}):
        with pytest.raises(RuntimeError):
            await sbx.call("job/start", **bad)
    with pytest.raises(RuntimeError, match="job tak ada"):
        await sbx.call("job/read", id="nope")


TREE = """
import subprocess, sys, time
# anak di SESI sendiri (seperti perintah yang dijalankan OpenCode), yang punya cucu
child = subprocess.Popen([sys.executable, "-c",
    "import subprocess,sys,time\\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\\n"
    "print(g.pid, flush=True)\\n"
    "time.sleep(60)"], start_new_session=True, stdout=subprocess.PIPE, text=True)
grand = int(child.stdout.readline())
print(child.pid, grand, flush=True)
time.sleep(60)
"""


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def wait_dead(*pids, seconds=8):
    end = time.time() + seconds
    while time.time() < end:
        if not any(pid_alive(p) for p in pids):
            return True
        await asyncio.sleep(0.1)
    return False


async def test_kill_reaches_processes_that_escaped_the_process_group(sb):
    sbx, _ = sb
    jid = (await sbx.call("job/start", argv=[sys.executable, "-c", TREE]))["id"]
    out = ""
    for _ in range(100):
        r = await sbx.call("job/read", id=jid, offset=0)
        out = r["data"]
        if out.strip():
            break
        await asyncio.sleep(0.1)
    child, grand = (int(x) for x in out.split())
    assert pid_alive(child) and pid_alive(grand)
    os.getpgid(child) != os.getpgid(os.getpid())          # di grup/sesi lain: killpg saja tak cukup

    await sbx.call("job/kill", id=jid)
    assert await wait_dead(child, grand), "proses turunan masih hidup setelah kill"
    _, last = await poll(sbx, jid)
    assert last["exit_code"] is not None


async def test_timeout_also_kills_the_whole_tree(sb):
    sbx, _ = sb
    jid = (await sbx.call("job/start", argv=[sys.executable, "-c", TREE], timeout=2))["id"]
    out = ""
    for _ in range(100):
        out = (await sbx.call("job/read", id=jid, offset=0))["data"]
        if out.strip():
            break
        await asyncio.sleep(0.1)
    child, grand = (int(x) for x in out.split())
    _, last = await poll(sbx, jid, timeout=15)
    assert last["timed_out"] is True
    assert await wait_dead(child, grand), "proses turunan masih hidup setelah timeout"
