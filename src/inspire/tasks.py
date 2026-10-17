"""Mode tugas coding: pekerjaan panjang di latar belakang, per proyek git di sandbox.

/proyek  pilih/buat folder proyek (/workspace/<nama>, repo git sendiri)
/tugas   jalankan tugas (batas langkah & waktu lebih besar, kabar kemajuan)
/stop    hentikan tugas yang berjalan
/diff    lihat perubahan satu tugas
/batalkan  kembalikan proyek ke kondisi sebelum tugas terbaru

Titik simpan: commit otomatis sebelum dan sesudah tiap tugas. Hanya sandbox;
tool server/DB/jadwal tidak dipakai di sini (lihat Agent.run_task).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import sqlite3
import time

from . import opencode as oc

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY,
    chat_id       TEXT NOT NULL,
    project       TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    status        TEXT NOT NULL,     -- berjalan | selesai | dihentikan | gagal | dibatalkan
    before_commit TEXT,
    after_commit  TEXT,
    started       REAL NOT NULL,
    ended         REAL,
    steps         INTEGER DEFAULT 0,
    journal_from  INTEGER DEFAULT 0, -- entri jurnal sandbox selama tugas: (from, to]
    journal_to    INTEGER DEFAULT 0,
    summary       TEXT,
    engine        TEXT DEFAULT 'inspire',   -- inspire | opencode
    session       TEXT,                     -- id sesi OpenCode (untuk /opencode lanjut)
    model         TEXT,
    tokens        INTEGER DEFAULT 0,
    cost          REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""
GITIGNORE = "__pycache__/\n*.pyc\n.venv/\nvenv/\nnode_modules/\n.pytest_cache/\n.DS_Store\n"
RESERVED = {"inbox"}
ICON = {"selesai": "✅", "dihentikan": "⏹", "gagal": "❌", "dibatalkan": "↩️", "berjalan": "⚙️"}
DIFF_INLINE_MAX = 6000


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def brief(tool: str, raw_args: str) -> str:
    """Ringkasan satu langkah untuk kabar kemajuan."""
    try:
        a = json.loads(raw_args or "{}")
    except ValueError:
        return tool
    if tool in ("write_file", "edit_file", "read_file"):
        verb = {"write_file": "tulis", "edit_file": "ubah", "read_file": "baca"}[tool]
        return f"{verb} {a.get('path', '?')}"
    if tool == "shell":
        return " ".join(str(a.get("command", "")).split())[:60]
    return tool


class Tasks:
    def __init__(self, db: sqlite3.Connection, agent, sandbox, journal, cfg):
        self.db = db
        self.agent, self.sandbox, self.journal, self.cfg = agent, sandbox, journal, cfg
        self.router = None            # diisi run(): send, send_code, send_file, confirm_for
        self._handle: asyncio.Task | None = None
        self.running_id: int | None = None
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(tasks)")}
        for name, ddl in (("engine", "TEXT DEFAULT 'inspire'"), ("session", "TEXT"), ("model", "TEXT"),
                          ("tokens", "INTEGER DEFAULT 0"), ("cost", "REAL DEFAULT 0")):
            if name not in cols:                      # DB dari versi sebelum OpenCode
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
        self.db.commit()
        self._oc_ready_at = 0.0                       # cache pengecekan "OpenCode terpasang di sandbox"
        self._oc_models: list[str] | None = None
        # Proses sebelumnya mati saat tugas jalan → jangan tampil "berjalan" selamanya.
        self.db.execute("UPDATE tasks SET status='dihentikan' WHERE status='berjalan'")
        self.db.commit()

    # --- kv & proyek ----------------------------------------------------------

    def _kv_get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _kv_set(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=?",
                        (key, value, value))
        self.db.commit()

    def active_project(self) -> str | None:
        return self._kv_get("project")

    async def sh(self, command: str, timeout: int = 60) -> tuple[int, str, str]:
        r = await self.sandbox.call("exec", command=command, timeout=timeout)
        return r["exit_code"], r["stdout"], r["stderr"]

    async def git(self, project: str, *args: str, timeout: int = 60) -> tuple[int, str, str]:
        # cwd perintah sandbox = workspace-nya, jadi path proyek relatif (tak bergantung lokasi workspace).
        cmd = (f"cd {shlex.quote(project)} && git -c user.name=inspire "
               f"-c user.email=inspire@localhost -c core.quotepath=false " + " ".join(shlex.quote(a) for a in args))
        return await self.sh(cmd, timeout)

    async def ensure_project(self, project: str) -> None:
        d = shlex.quote(project)
        code, _, err = await self.sh(
            f"mkdir -p {d} && cd {d} && {{ [ -d .git ] || git init -q; }} && "
            f"{{ [ -f .gitignore ] || printf {shlex.quote(GITIGNORE)} > .gitignore; }}"
        )
        if code != 0:
            raise RuntimeError(f"gagal menyiapkan proyek: {err.strip()[:200]}")

    async def checkpoint(self, project: str, message: str) -> str:
        code, out, err = await self.git(project, "add", "-A")
        if code == 0:
            code, out, err = await self.git(project, "commit", "-q", "--allow-empty", "-m", message)
        if code != 0:
            raise RuntimeError(f"git commit gagal: {err.strip()[:200]}")
        _, head, _ = await self.git(project, "rev-parse", "HEAD")
        return head.strip()

    async def projects(self) -> list[str]:
        _, out, _ = await self.sh("for d in */; do [ -d \"$d\" ] && echo \"${d%/}\"; done")
        return [p for p in out.split() if p not in RESERVED]

    # --- perintah -----------------------------------------------------------------

    async def command(self, chat_id: str, name: str, args: list[str], confirm=None, prefix: str = "/") -> str:
        if not self.sandbox:
            return "Mode tugas butuh sandbox (mode Docker). Lihat `inspire status`."
        try:
            if name == "proyek":
                return await self._cmd_proyek(args, prefix)
            if name == "tugas":
                return await self._cmd_tugas(chat_id, " ".join(args), prefix)
            if name == "opencode":
                return await self._cmd_opencode(chat_id, args, prefix)
            if name == "stop":
                return self.stop()
            if name == "diff":
                return await self._cmd_diff(chat_id, args)
            if name == "batalkan":
                return await self._cmd_batalkan(chat_id, args, confirm)
        except RuntimeError as e:
            return f"⚠️ {e}"
        return ""

    async def _cmd_proyek(self, args: list[str], p: str) -> str:
        if args[:1] in (["baru"], ["pakai"]):
            name = slug(" ".join(args[1:]))
            if not name or name in RESERVED:
                return f"Nama proyek tak valid. Pakai: {p}proyek {args[0]} <nama>"
            if args[0] == "pakai" and name not in await self.projects():
                return f"Proyek '{name}' tak ada. Buat dulu: {p}proyek baru {name}"
            await self.ensure_project(name)
            self._kv_set("project", name)
            return f"📁 Proyek aktif: {name} (/workspace/{name}, git aktif)."
        active, names = self.active_project(), await self.projects()
        if not names:
            return f"Belum ada proyek. Buat: {p}proyek baru <nama>\nAtau langsung {p}tugas <deskripsi> (proyek dibuat otomatis)."
        listing = "\n".join(f"{'▶' if n == active else '•'} {n}" for n in names)
        return f"Proyek:\n{listing}\n\n{p}proyek baru <nama> · {p}proyek pakai <nama>"

    async def _cmd_tugas(self, chat_id: str, prompt: str, p: str) -> str:
        if not prompt.strip():
            recent = self.db.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 8").fetchall()
            head = (f"Pakai: {p}tugas <deskripsi pekerjaan>\nContoh: {p}tugas buat skrip python hitung BMI "
                    f"lengkap dengan tes\nProyek aktif: {self.active_project() or '(belum ada, dibuat otomatis)'}")
            if not recent:
                return head
            lines = [f"{ICON.get(r['status'], '•')} #{r['id']} [{r['project']}] {r['status']} · {r['prompt'][:60]}"
                     for r in recent]
            return head + "\n\nTugas terakhir:\n" + "\n".join(lines)
        return await self._start(chat_id, prompt, p)

    async def _start(self, chat_id: str, prompt: str, p: str, engine: str = "inspire",
                     session: str = "", model: str = "") -> str:
        if self.running_id:
            return f"Tugas #{self.running_id} masih berjalan. {p}stop untuk menghentikan."
        project = self.active_project() or slug(" ".join(prompt.split()[:3])) or "proyek"
        if project in RESERVED:
            project = "proyek"
        await self.ensure_project(project)
        self._kv_set("project", project)
        cur = self.db.execute(
            "INSERT INTO tasks(chat_id, project, prompt, status, started, engine, model) VALUES (?,?,?,?,?,?,?)",
            (chat_id, project, prompt.strip(), "berjalan", time.time(), engine, model or None))
        self.db.commit()
        tid = cur.lastrowid
        before = await self.checkpoint(project, f"sebelum tugas #{tid}")
        jfrom = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM journal").fetchone()[0]
        self.db.execute("UPDATE tasks SET before_commit=?, journal_from=? WHERE id=?", (before, jfrom, tid))
        self.db.commit()
        if engine == "opencode":   # catatan audit: file yang diubah OpenCode tak dijurnal satu per satu
            self.journal.add(chat_id, "sandbox", "opencode", f"OpenCode · tugas #{tid}: {prompt.strip()[:80]}")
        self.running_id = tid
        self._handle = asyncio.create_task(self._run(tid, chat_id, project, prompt.strip(), engine, session, model))
        if engine == "opencode":
            return (f"▶ Tugas #{tid} (OpenCode · {model or 'model otomatis'}) dimulai di proyek '{project}'. "
                    f"Berjalan di latar belakang (maks {self.cfg.opencode_max_steps} langkah / "
                    f"{self.cfg.task_max_minutes} menit), kabar berkala. {p}stop untuk menghentikan.")
        return (f"▶ Tugas #{tid} dimulai di proyek '{project}'. Aku bekerja di latar belakang "
                f"(maks {self.cfg.task_max_iterations} langkah / {self.cfg.task_max_minutes} menit) dan "
                f"mengabari berkala. Kamu tetap bisa ngobrol. {p}stop untuk menghentikan.")

    # --- OpenCode -------------------------------------------------------------------------

    def oc_model(self) -> str:
        return self._kv_get("oc_model") or self.cfg.opencode_model or ""

    async def oc_ready(self) -> tuple[bool, str]:
        """OpenCode terpasang di sandbox? (di-cache 10 menit). → (ok, versi / pesan)."""
        if self._oc_ready_at and time.time() - self._oc_ready_at < 600 and self._oc_version:
            return True, self._oc_version
        try:
            code, out, err = await oc.capture(self.sandbox, [self.cfg.opencode_bin, "--version"], timeout=30)
        except RuntimeError as e:
            return False, str(e)
        if code != 0 or not out.strip():
            return False, (err or out).strip()[:200] or "tak terpasang"
        self._oc_version, self._oc_ready_at = out.strip().splitlines()[-1], time.time()
        return True, self._oc_version

    _oc_version = ""

    async def oc_models(self, refresh: bool = False) -> list[str]:
        if self._oc_models is None or refresh:
            env = oc.build_env(self.cfg.opencode_key)
            code, out, _ = await oc.capture(self.sandbox, [self.cfg.opencode_bin, "models"], env=env, timeout=90)
            self._oc_models = oc.parse_models(out) if code == 0 else []
        return self._oc_models

    async def _cmd_opencode(self, chat_id: str, args: list[str], p: str) -> str:
        sub = args[0].lower() if args else ""
        ok, info = await self.oc_ready()
        if not ok:
            return (f"OpenCode belum bisa dipakai di sandbox ({info}). Image sandbox mungkin lama: jalankan "
                    "`inspire update` di server (membangun ulang image).")
        if not args:
            key = "ada (model berbayar)" if self.cfg.opencode_key else "tidak ada (model gratis bawaan OpenCode)"
            return (f"🧰 OpenCode {info} di sandbox\nModel: {self.oc_model() or 'otomatis (OpenCode memilih)'} · key: {key}\n"
                    f"Proyek aktif: {self.active_project() or '(dibuat otomatis)'}\n\n"
                    f"{p}opencode <tugas> — kerjakan di latar belakang (titik simpan git, {p}diff, {p}batalkan)\n"
                    f"{p}opencode lanjut <pesan> — lanjutkan sesi OpenCode tugas terakhir di proyek ini\n"
                    f"{p}opencode models — daftar model · {p}opencode model <provider/model|auto> — pilih model")
        if sub == "models":
            models = await self.oc_models(refresh=True)
            if not models:
                return "Daftar model kosong (OpenCode belum bisa mengambilnya). Coba lagi nanti."
            cur = self.oc_model()
            lines = [f"{'▶ ' if m == cur else '  '}{m}" for m in models[:60]]
            more = f"\n… dan {len(models) - 60} lagi" if len(models) > 60 else ""
            note = "" if self.cfg.opencode_key else "\n(Tanpa key hanya model gratis bawaan OpenCode yang tampil.)"
            return (f"Model OpenCode ({len(models)}):\n" + "\n".join(lines) + more + note +
                    f"\n\n{p}opencode model <provider/model> untuk memilih")
        if sub == "model":
            if len(args) < 2:
                return f"Model sekarang: {self.oc_model() or 'otomatis'}. Pilih: {p}opencode model <provider/model> atau auto"
            want = args[1]
            if want.lower() in ("auto", "reset", "otomatis"):
                self._kv_set("oc_model", "")
                self.cfg.opencode_model = ""
                return "Model OpenCode: otomatis (OpenCode memilih sendiri)."
            known = await self.oc_models()
            if known and want not in known:
                close = [m for m in known if want.lower() in m.lower()][:6]
                hint = ("\nMirip: " + ", ".join(close)) if close else f"\nLihat daftar: {p}opencode models"
                return f"Model '{want}' tak ada di daftar OpenCode.{hint}"
            self._kv_set("oc_model", want)
            return f"Model OpenCode: {want}"
        if sub == "lanjut":
            text = " ".join(args[1:]).strip()
            if not text:
                return f"Pakai: {p}opencode lanjut <pesan lanjutan>"
            project = self.active_project()
            row = self.db.execute(
                "SELECT session FROM tasks WHERE engine='opencode' AND project=? AND session IS NOT NULL AND session!='' "
                "ORDER BY id DESC LIMIT 1", (project,)).fetchone() if project else None
            if not row:
                return f"Belum ada sesi OpenCode di proyek ini. Mulai dengan {p}opencode <tugas>."
            return await self._start(chat_id, text, p, "opencode", row["session"], self.oc_model())
        return await self._start(chat_id, " ".join(args), p, "opencode", "", self.oc_model())

    def stop(self) -> str:
        if not self.running_id or not self._handle:
            return "Tak ada tugas yang berjalan."
        self._handle.cancel()
        return (f"⏹ Menghentikan tugas #{self.running_id}… (perintah shell yang sedang jalan di sandbox "
                "selesai sendiri sesuai timeout-nya)")

    # --- eksekusi latar belakang -----------------------------------------------------

    async def _run(self, tid: int, chat_id: str, project: str, prompt: str,
                   engine: str = "inspire", session: str = "", model: str = "") -> None:
        started = time.monotonic()
        state = {"steps": 0, "last": started, "recent": []}

        async def note(text: str, n: int) -> None:
            state["steps"] = n
            state["recent"].append(text)
            if time.monotonic() - state["last"] >= self.cfg.task_progress_seconds:
                state["last"] = time.monotonic()
                recent, state["recent"] = state["recent"][-3:], []
                await self._say(chat_id, f"⚙️ Tugas #{tid} · langkah {n} · " + "; ".join(recent))

        async def on_step(tool: str, raw: str, n: int) -> None:
            await note(brief(tool, raw), n)

        status, reply, extra = "selesai", "", None
        limit_s = self.cfg.task_max_minutes * 60
        try:
            if engine == "opencode":
                out = await asyncio.wait_for(
                    oc.run(self.sandbox, project, prompt, binary=self.cfg.opencode_bin, model=model, session=session,
                           key=self.cfg.opencode_key, timeout=limit_s, max_steps=self.cfg.opencode_max_steps,
                           poll=self.cfg.opencode_poll, on_step=note),
                    timeout=limit_s + 30)                      # cadangan; job sandbox punya timeout sendiri
                status, reply, extra = self._oc_result(out, model)
                state["steps"] = max(state["steps"], out.summary.steps)
                self.db.execute("UPDATE tasks SET session=?, tokens=?, cost=? WHERE id=?",
                                (out.summary.session or None, out.summary.tokens_in + out.summary.tokens_out,
                                 out.summary.cost, tid))
                self.db.commit()
            else:
                confirm = self.router.confirm_for(chat_id) if self.router else None
                reply, _ = await asyncio.wait_for(
                    self.agent.run_task(chat_id, tid, project, prompt, confirm, on_step), timeout=limit_s)
                if reply.startswith("⚠️ Semua model gagal"):
                    status = "gagal"
        except asyncio.TimeoutError:
            status, reply = "dihentikan", f"⚠️ Waktu {self.cfg.task_max_minutes} menit habis. Pekerjaan sampai di sini tersimpan."
        except asyncio.CancelledError:
            status, reply = "dihentikan", "Dihentikan olehmu (/stop). Pekerjaan sampai di sini tersimpan."
        except Exception as e:
            log.exception("tugas #%s gagal", tid)
            status, reply = "gagal", f"{type(e).__name__}: {e}"
        try:
            await self._finish(tid, chat_id, project, status, reply, state["steps"], time.monotonic() - started, extra)
        except Exception:
            log.exception("finalisasi tugas #%s gagal", tid)
            await self._say(chat_id, f"❌ Tugas #{tid} berakhir, tapi titik simpan akhir gagal dibuat. Cek /diff {tid}.")
        finally:
            self.running_id, self._handle = None, None

    def _oc_result(self, out: "oc.Outcome", model: str) -> tuple[str, str, dict]:
        s = out.summary
        extra = {"engine": "OpenCode", "model": model or "otomatis", "tokens": s.tokens_in + s.tokens_out,
                 "cost": s.cost, "failed": s.failed_tools}
        final = s.final_text
        if out.limited:
            return "dihentikan", (f"⚠️ Melewati batas {self.cfg.opencode_max_steps} langkah, dihentikan. Pekerjaan sampai "
                                  f"di sini tersimpan; lanjutkan dengan /opencode lanjut …\n\n{final}").strip(), extra
        if out.timed_out:
            return "dihentikan", f"⚠️ Waktu {self.cfg.task_max_minutes} menit habis. Pekerjaan sampai di sini tersimpan.\n\n{final}".strip(), extra
        if s.error:
            return "gagal", oc.explain_error(s.error), extra
        if out.exit_code in (-9, 137):
            return "gagal", ("OpenCode dimatikan (kode 137): hampir pasti kehabisan memori. Batas sandbox bawaan 1 GB; "
                             "naikkan dengan SANDBOX_MEM di .env lalu `docker compose up -d`, atau pilih tugas yang lebih kecil."), extra
        if out.exit_code not in (0, None):
            tail = (out.stderr or "").strip()[-300:]
            return "gagal", f"OpenCode berhenti dengan kode {out.exit_code}.\n{tail}\n{final}".strip(), extra
        return "selesai", final or "(OpenCode tak memberi ringkasan; lihat /diff)", extra

    async def _finish(self, tid, chat_id, project, status, reply, steps, seconds, extra=None) -> None:
        after = await self.checkpoint(project, f"tugas #{tid}")
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        jto = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM journal").fetchone()[0]
        self.db.execute(
            "UPDATE tasks SET status=?, after_commit=?, ended=?, steps=?, journal_to=?, summary=? WHERE id=?",
            (status, after, time.time(), steps, jto, reply[:4000], tid))
        self.db.commit()
        _, stat, _ = await self.git(project, "diff", "--stat", row["before_commit"], after)
        mins = max(1, round(seconds / 60))
        head = f"{ICON[status]} Tugas #{tid} {status} · {steps} langkah · ±{mins} menit"
        if extra:
            head += f"\n🧰 {extra['engine']} · {extra['model']} · {extra['tokens']:,} token".replace(",", ".")
            head += f" · ${extra['cost']:.4f}" if extra["cost"] else ""
            head += f" · {extra['failed']} langkah gagal" if extra["failed"] else ""
        text = (f"{head}\n\n{reply}\n\n"
                f"Perubahan:\n{stat.strip() or '(tak ada perubahan file)'}\n\n/diff {tid} · /batalkan {tid}")
        await self._say(chat_id, text)

    async def _say(self, chat_id: str, text: str) -> None:
        if self.router:
            try:
                await self.router.send(chat_id, text)
            except Exception:
                log.exception("gagal mengirim kabar tugas")

    # --- diff & batalkan ---------------------------------------------------------------

    def _pick(self, args: list[str], undoable: bool = False):
        if args and args[0].lstrip("#").isdigit():
            return self.db.execute("SELECT * FROM tasks WHERE id=?", (int(args[0].lstrip("#")),)).fetchone()
        sql = "SELECT * FROM tasks WHERE after_commit IS NOT NULL" + (" AND status!='dibatalkan'" if undoable else "")
        return self.db.execute(sql + " ORDER BY id DESC LIMIT 1").fetchone()

    async def _cmd_diff(self, chat_id: str, args: list[str]) -> str:
        t = self._pick(args)
        if not t or not t["after_commit"]:
            return "Belum ada tugas selesai untuk dilihat diff-nya."
        code, diff, err = await self.git(t["project"], "diff", t["before_commit"], t["after_commit"])
        if code != 0:
            return f"Diff gagal: {err.strip()[:200]} (riwayat git proyek berubah?)"
        _, stat, _ = await self.git(t["project"], "diff", "--stat", t["before_commit"], t["after_commit"])
        head = f"📄 Tugas #{t['id']} [{t['project']}] · {t['status']}\n{stat.strip() or '(tak ada perubahan file)'}"
        if not diff.strip() or not self.router:
            return head
        await self.router.send(chat_id, head)
        if len(diff) <= DIFF_INLINE_MAX:
            await self.router.send_code(chat_id, diff)
        else:
            await self.router.send_file(chat_id, f"tugas-{t['id']}.diff", diff.encode(), "Diff lengkap")
        return ""

    async def _cmd_batalkan(self, chat_id: str, args: list[str], confirm) -> str:
        if self.running_id:
            return f"Tugas #{self.running_id} masih berjalan. /stop dulu."
        t = self._pick(args, undoable=True)
        if not t:
            return "Tak ada tugas yang bisa dibatalkan."
        latest = self.db.execute(
            "SELECT id FROM tasks WHERE project=? AND after_commit IS NOT NULL AND status!='dibatalkan' "
            "ORDER BY id DESC LIMIT 1", (t["project"],)).fetchone()
        if t["status"] == "dibatalkan":
            return f"Tugas #{t['id']} sudah dibatalkan."
        if latest["id"] != t["id"]:
            return (f"Hanya tugas terbaru di proyek '{t['project']}' yang bisa dibatalkan: #{latest['id']}. "
                    f"Batalkan #{latest['id']} dulu, atau /batalkan {latest['id']}.")
        code, _, _ = await self.git(t["project"], "cat-file", "-e", f"{t['before_commit']}^{{commit}}")
        if code != 0:
            return "Titik simpan sebelum tugas ini sudah tak ada (riwayat git proyek diubah)."
        _, head, _ = await self.git(t["project"], "rev-parse", "HEAD")
        _, dirty, _ = await self.git(t["project"], "status", "--porcelain")
        warn = ""
        if head.strip() != t["after_commit"] or dirty.strip():
            warn = "\n\n⚠️ Ada perubahan SESUDAH tugas ini (di luar tugas); itu ikut hilang."
        _, stat, _ = await self.git(t["project"], "diff", "--stat", t["before_commit"], t["after_commit"])
        if not confirm or not await confirm(
            f"⏪ Batalkan tugas #{t['id']} [{t['project']}]?\n{t['prompt'][:100]}\n\n"
            f"Proyek kembali ke kondisi sebelum tugas:\n{stat.strip() or '(tak ada perubahan)'}{warn}"
        ):
            return "Pembatalan dibatalkan."
        code, _, err = await self.git(t["project"], "reset", "--hard", t["before_commit"])
        if code == 0:
            await self.git(t["project"], "clean", "-fd")
        if code != 0:
            return f"❌ Gagal membatalkan: {err.strip()[:200]}"
        new_id = self.journal.add(chat_id, "sandbox", "task_undo", f"batalkan tugas #{t['id']}: {t['prompt'][:60]}")
        self.db.execute(
            "UPDATE journal SET status='dibatalkan', undone_by=? WHERE id>? AND id<=? AND id!=? AND place='sandbox' "
            "AND undone_by IS NULL AND tool!='task_undo'", (new_id, t["journal_from"], t["journal_to"], new_id))
        self.db.execute("UPDATE tasks SET status='dibatalkan' WHERE id=?", (t["id"],))
        self.db.commit()
        return f"⏪ Tugas #{t['id']} dibatalkan. Proyek '{t['project']}' kembali ke kondisi sebelumnya."
