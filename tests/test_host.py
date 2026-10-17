"""Tool server diuji pada "server" lokal: bash, git, dan sqlite sungguhan di folder sementara."""

import os
import sqlite3
import subprocess

import pytest

from inspire.tools import Registry, ToolContext
from inspire.tools.db import check_sql, mask_rows, register_db
from inspire.tools.host import Host, Sites, _exec, host_needs_confirm, mask_env, register_host

ENV = """APP_NAME=Perizinan
APP_KEY=base64:SANGATRAHASIA123456
APP_URL=https://izin.example.go.id
DB_CONNECTION=sqlite
DB_DATABASE=database/app.sqlite
DB_PASSWORD=pw-rahasia-99
MAIL_PASSWORD=surat-rahasia
"""


class LocalHost(Host):
    def __init__(self, home):
        super().__init__("local", 22, "me", "", "")
        self.home = home
        self.scripts = []

    def as_owner(self, owner, script):
        return script

    async def run(self, script, stdin=None, timeout=120):
        self.scripts.append(script)
        return await _exec(["bash", "-c", script], stdin, timeout, env={**os.environ, "HOME": str(self.home)})


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "www" / "izin"
    (root / "app").mkdir(parents=True)
    (root / "database").mkdir()
    (root / "storage" / "logs").mkdir(parents=True)
    (root / ".env").write_text(ENV)
    (root / "app" / "Ctrl.php").write_text("<?php\nreturn 'lama';\n")
    (root / "storage" / "logs" / "laravel.log").write_text("ERROR: koneksi pw-rahasia-99 gagal\n")
    db = sqlite3.connect(root / "database" / "app.sqlite")
    db.execute("CREATE TABLE pemohon (id INTEGER, nik TEXT, nama TEXT, status TEXT)")
    db.executemany("INSERT INTO pemohon VALUES (?,?,?,?)",
                   [(i, f"14710{i:011d}", f"Warga {i}", "proses") for i in range(30)])
    db.commit()
    db.close()
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    (root / ".gitignore").write_text(".env\n")
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "awal"], check=True)

    host = LocalHost(tmp_path / "home")
    sites = Sites(host, [{"name": "izin", "path": str(root), "owner": "me"}])
    reg = Registry()
    register_host(reg, sites)
    register_db(reg, sites)
    return reg, root, host


def confirmer(answer=True):
    asked = []

    async def confirm(text, **kw):
        asked.append((text, kw))
        return answer

    return confirm, asked


def test_host_command_policy():
    for ok in ("tail -n 50 storage/logs/laravel.log", "git status", "git log --oneline -5",
               "php artisan route:list --path=izin", "php artisan about", "ls -la && cat composer.json",
               "curl -sS -o /dev/null -w '%{http_code}' https://x 2>&1", "systemctl status nginx",
               "grep -rn foo app"):
        assert host_needs_confirm(ok) is None, ok
    for risky in ("php artisan migrate --force", "rm -rf storage", "composer install", "git pull",
                  "git checkout main", "curl -X POST https://x", "echo x > a", "npm run build",
                  "php artisan tinker", "git branch -D x", "git remote add o u", "find . -delete"):
        assert host_needs_confirm(risky), risky


def test_mask_env():
    assert mask_env("A=1\n# komentar\nB=\nexport C=rahasia") == "A=***\n# komentar\nB=\nexport C=***"


async def test_read_masks_secrets(site):
    reg, root, _ = site
    ctx = ToolContext("1", 1)
    env = await reg.call("host_read", {"site": "izin", "path": ".env"}, ctx)
    assert "APP_URL=***" in env and "DB_PASSWORD=***" in env and "SANGAT" not in env
    log = await reg.call("host_read", {"site": "izin", "path": "storage/logs/laravel.log"}, ctx)
    assert "koneksi *** gagal" in log                    # nilai rahasia disamarkan di file lain
    run = await reg.call("host_run", {"site": "izin", "command": "cat .env | grep APP"}, ctx)
    assert "APP_KEY=***" in run and "SANGAT" not in run
    run = await reg.call("host_run", {"site": "izin", "command": "grep -r rahasia storage"}, ctx)
    assert "pw-rahasia" not in run


async def test_paths_confined(site, tmp_path):
    reg, root, _ = site
    ctx = ToolContext("1", 1)
    for p in ("../../etc/passwd", "/etc/passwd"):
        assert "di luar folder situs" in await reg.call("host_read", {"site": "izin", "path": p}, ctx)
    os.symlink("/etc", root / "etc-link")
    assert "di luar folder situs" in await reg.call("host_read", {"site": "izin", "path": "etc-link/hosts"}, ctx)
    assert "tak terdaftar" in await reg.call("host_read", {"site": "lain", "path": "x"}, ctx)


async def test_edit_shows_diff_snapshots_and_rolls_back(site):
    reg, root, _ = site
    confirm, asked = confirmer(True)
    ctx = ToolContext("1", 1, confirm=confirm)
    out = await reg.call("host_edit", {"site": "izin", "path": "app/Ctrl.php", "old": "'lama'", "new": "'baru'"}, ctx)
    assert "Tersimpan" in out and "snapshot: git:" in out
    assert "-return 'lama';" in asked[0][0] and "+return 'baru';" in asked[0][0]
    assert asked[0][1] == {}                                  # tanpa tombol izin 30 menit
    assert (root / "app/Ctrl.php").read_text() == "<?php\nreturn 'baru';\n"

    snaps = await reg.call("host_rollback", {"site": "izin"}, ctx)
    snap = next(line for line in snaps.splitlines() if line.startswith("git:"))
    assert "exit_code: 0" in await reg.call("host_rollback", {"site": "izin", "snapshot": snap}, ctx)
    assert (root / "app/Ctrl.php").read_text() == "<?php\nreturn 'lama';\n"


async def test_edit_rejected_and_secret_files(site):
    reg, root, _ = site
    confirm, _ = confirmer(False)
    ctx = ToolContext("1", 1, confirm=confirm)
    out = await reg.call("host_edit", {"site": "izin", "path": "app/Ctrl.php", "old": "'lama'", "new": "'x'"}, ctx)
    assert "TIDAK diubah" in out and "lama" in (root / "app/Ctrl.php").read_text()
    assert "file rahasia" in await reg.call("host_write", {"site": "izin", "path": ".env", "content": "x"}, ctx)
    job = ToolContext("1", 1, confirm=confirm, is_job=True)
    assert "jadwal" in await reg.call("host_run", {"site": "izin", "command": "rm -f x"}, job)


async def test_run_mutating_needs_confirm_and_snapshots(site):
    reg, root, _ = site
    confirm, asked = confirmer(True)
    ctx = ToolContext("1", 1, confirm=confirm)
    out = await reg.call("host_run", {"site": "izin", "command": "touch storage/baru.txt"}, ctx)
    assert asked and "SERVER · izin" in asked[0][0] and "snapshot: git:" in out
    assert (root / "storage/baru.txt").exists()


async def test_non_git_site_backs_up_file(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "index.php").write_text("v1\n")
    host = LocalHost(tmp_path / "home")
    reg = Registry()
    register_host(reg, Sites(host, [{"name": "plain", "path": str(root), "owner": "me"}]))
    confirm, _ = confirmer(True)
    ctx = ToolContext("1", 1, confirm=confirm)
    out = await reg.call("host_write", {"site": "plain", "path": "index.php", "content": "v2\n"}, ctx)
    assert "snapshot: file:" in out and (root / "index.php").read_text() == "v2\n"
    snap = out.split("snapshot: ")[1].split(" ")[0]
    await reg.call("host_rollback", {"site": "plain", "snapshot": snap}, ctx)
    assert (root / "index.php").read_text() == "v1\n"


async def test_reveal_needs_button(site):
    reg, _, _ = site
    no, _ = confirmer(False)
    assert "tetap tersembunyi" in await reg.call("host_reveal", {"site": "izin", "key": "APP_URL"}, ToolContext("1", 1, confirm=no))
    yes, _ = confirmer(True)
    assert await reg.call("host_reveal", {"site": "izin", "key": "APP_URL"},
                          ToolContext("1", 1, confirm=yes)) == "APP_URL=https://izin.example.go.id"


# --- database ---------------------------------------------------------------

def test_check_sql():
    assert check_sql("SHOW TABLES;")[0] == "struktur"
    assert check_sql("select * from t")[0] == "data"
    for bad in ("DELETE FROM t", "SELECT 1; DROP TABLE t", "UPDATE t SET a=1",
                "SELECT * FROM t INTO OUTFILE '/tmp/x'", "SELECT sleep(10)", "SELECT * FROM t FOR UPDATE", ""):
        with pytest.raises(ValueError):
            check_sql(bad)


def test_mask_rows():
    table, hidden = mask_rows("id\tnik\tnama_lengkap\tstatus\n1\t123\tBudi\tok\n", 20)
    assert table.splitlines()[1] == "1\t***\t***\tok" and hidden == ["nik", "nama_lengkap"]


async def test_db_query_sqlite(site):
    reg, _, host = site
    confirm, asked = confirmer(True)
    ctx = ToolContext("1", 1, confirm=confirm)
    out = await reg.call("db_query", {"site": "izin", "sql": "SELECT * FROM pemohon"}, ctx)
    rows = out.split("\n(kolom")[0].splitlines()
    assert rows[0] == "id\tnik\tnama\tstatus" and len(rows) == 21          # header + 20
    assert rows[1] == "0\t***\t***\tproses" and "Warga" not in out and "nik, nama" in out
    assert asked and "SELECT * FROM pemohon" in asked[0][0]
    assert all("pw-rahasia" not in s for s in host.scripts)               # password tak di argumen

    no, _ = confirmer(False)
    assert "TIDAK dijalankan" in await reg.call("db_query", {"site": "izin", "sql": "SELECT 1"},
                                                ToolContext("1", 1, confirm=no))
    assert "Ditolak" in await reg.call("db_query", {"site": "izin", "sql": "DELETE FROM pemohon"}, ctx)


async def test_db_readonly_enforced(site):
    """Walau lolos pemeriksaan teks, sqlite -readonly tetap menolak menulis."""
    _, root, host = site
    from inspire.tools.db import build

    script, stdin = build({"DB_CONNECTION": "sqlite", "DB_DATABASE": "database/app.sqlite"},
                          "DELETE FROM pemohon", "struktur")
    code, _, err = await host.run(f"cd {root} && {script}", stdin.encode())
    assert code != 0 and "readonly" in err.lower()


# --- setup web ---------------------------------------------------------------

def test_setup_web_flow(tmp_path, monkeypatch):
    from inspire import build
    from inspire.settings import Settings
    from inspire.setup import status, wizard
    from inspire.tools import host as host_mod

    s = Settings(tmp_path / "data")
    answers = iter(["/var/www/perizinan", "", ""])          # path, nama (bawaan), selesai
    monkeypatch.setattr(wizard, "ask", lambda prompt, default="": next(answers) or default)
    monkeypatch.setattr(wizard, "yes", lambda prompt, default=True: True)
    monkeypatch.setenv("INSPIRE_MODE", "docker")
    wizard.step_web(s)
    assert (s.dir / "ssh" / "id_ed25519").exists()
    assert (s.dir / "ssh" / "sites.txt").read_text() == "perizinan|/var/www/perizinan\n"
    assert s.get("host.address") == "host.docker.internal"
    # sebelum langkah sudo: dicatat tapi tool belum aktif
    assert "host_run" not in build(s).agent.tools.tools
    import asyncio
    f = {x.id: x for x in asyncio.run(status(s))}
    assert f["web"].state == "err"

    # setup-web.sh menulis owners.txt; web-test mencatat pemilik & menguji SSH
    (s.dir / "ssh" / "owners.txt").write_text("perizinan|deploy\n")

    async def fake_run(self, script, stdin=None, timeout=120):
        assert "sudo -n -u deploy" in script
        return 0, "ok\nDB_CONNECTION=mysql\n", ""

    monkeypatch.setattr(host_mod.Host, "run", fake_run)
    wizard.step_web_test(s)
    assert s.get("host.sites") == [{"name": "perizinan", "path": "/var/www/perizinan", "owner": "deploy"}]
    tools = build(s).agent.tools.tools
    assert {"host_run", "host_edit", "db_query", "host_rollback"} <= set(tools)
    f = {x.id: x for x in asyncio.run(status(s))}
    assert f["web"].state == "on"
