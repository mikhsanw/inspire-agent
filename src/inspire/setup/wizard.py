"""Wizard terminal: `inspire setup [bagian]`. Enter = lewati."""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .. import catalog as catalog_mod
from .. import setup as S
from ..settings import Settings
from . import checks

TTY = sys.stdout.isatty()


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if TTY else text


def say(text: str = "") -> None:
    print(text, flush=True)


def ok(text: str) -> None:
    say(c("✅ " + text, "32"))


def bad(text: str) -> None:
    say(c("❌ " + text, "31"))


def head(text: str) -> None:
    say()
    say(c(f"── {text} ", "1;36") + c("─" * max(0, 50 - len(text)), "2"))


def ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{hint}: ").strip()
    except EOFError:
        val = ""
    return val or default


def ask_secret(prompt: str) -> str:
    try:
        return getpass.getpass(f"{prompt} (tak terlihat saat ditempel, Enter = lewati): ").strip()
    except EOFError:
        return ""


def yes(prompt: str, default: bool = True) -> bool:
    ans = ask(f"{prompt} {'[Y/n]' if default else '[y/N]'}").lower()
    return default if not ans else ans.startswith("y")


def choose(prompt: str, options: list[str], default: int = 1, allow_skip: bool = True) -> int | None:
    for i, o in enumerate(options, 1):
        say(f"  {i}) {o}")
    if allow_skip:
        say("  0) Lewati")
    while True:
        ans = ask(prompt, str(default))
        if ans.isdigit() and (int(ans) == 0 and allow_skip):
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return int(ans) - 1
        say("Pilih nomor dari daftar.")


def run(coro):
    return asyncio.run(coro)


# --- langkah ----------------------------------------------------------------


def step_key(s: Settings, sid: str) -> bool:
    sec = S.KEY_SECTIONS[sid]
    head(sec.title)
    say(sec.why)
    if s.key(sec.key):
        say(f"Sudah ada key {sec.title}.")
        if not yes("Ganti?", False):
            return True
    say(f"Ambil di: {c(sec.url, '4')}")
    while True:
        val = ask_secret(f"Tempel {sec.key}")
        if not val:
            s.skip(sid)
            say(c("⏸ dilewati — isi nanti: inspire setup " + sid + " / Telegram /setup", "33"))
            return False
        res, msg = run(S.save_key(s, sid, val))
        if res:
            ok(msg)
            return True
        bad(msg)


def step_pair_telegram(s: Settings) -> None:
    if s.owners("telegram"):
        ok(f"Telegram sudah punya pemilik ({', '.join(map(str, s.owners('telegram')))}).")
        return
    token = s.key("TELEGRAM_BOT_TOKEN")
    good, username = run(checks.telegram(token))
    if not good:
        return
    code = s.pairing_code()
    head("Pairing")
    say("Supaya bot hanya menurut padamu. Di HP, buka:")
    say("  " + c(f"https://t.me/{username.lstrip('@')}?start={code}", "1;4"))
    say(f"lalu tekan START. (Atau kirim: /pair {code})")
    say(c("Menunggu… (Ctrl+C = lewati, pairing bisa nanti)", "2"))
    try:
        user = run(_wait_pair(token, code))
    except KeyboardInterrupt:
        say()
        say(c(f"⏸ Pairing dilewati. Nanti kirim /pair {code} ke bot.", "33"))
        return
    if user is None:
        say(c(f"Bot sedang jalan di tempat lain. Kirim /pair {code} langsung ke bot.", "33"))
        return
    s.add_owner("telegram", user["id"])
    ok(f"Terhubung dengan {user.get('first_name', '')} (@{user.get('username', '-')}).")


async def _wait_pair(token: str, code: str, minutes: int = 10) -> dict | None:
    """Baca getUpdates sampai ada '/start CODE' atau '/pair CODE'. None = bentrok (409)."""
    api = f"https://api.telegram.org/bot{token}"
    offset = 0
    async with httpx.AsyncClient(timeout=40) as client:
        for _ in range(minutes * 2):
            r = await client.get(f"{api}/getUpdates", params={"timeout": 30, "offset": offset})
            if r.status_code == 409:
                return None
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                words = (msg.get("text") or "").split()
                if words[:1] in (["/start"], ["/pair"]) and words[1:2] == [code]:
                    await client.get(f"{api}/getUpdates", params={"offset": offset, "timeout": 0})
                    await client.post(f"{api}/sendMessage", json={
                        "chat_id": msg["chat"]["id"],
                        "text": "✅ Terhubung! Kamu pemilik bot ini. Selesaikan setup di terminal, "
                                "atau nanti ketik /setup di sini.",
                    })
                    return msg["from"]
    raise KeyboardInterrupt  # kelamaan → anggap dilewati


PAGE = 12


def _catalog(s: Settings, force: bool = False):
    say(c("Mengambil katalog model (models.dev)…", "2"))
    cat = run(catalog_mod.load(s.dir / "catalog.json", force=force))
    if not cat:
        bad("Katalog tak bisa diambil (periksa koneksi). Pakai 'Endpoint sendiri' dulu, atau coba lagi nanti.")
    return cat


def _pick_provider(cat):
    """→ CProvider | 'custom' | None (lewati/kembali)."""
    popular = cat.popular() if cat else []
    for i, p in enumerate(popular, 1):
        free = len(cat.models(p.id, free=True))
        note = f"{free} model $0" if free else ("berbayar" if p.id in catalog_mod.LOCKED_FREE else "")
        say(f"  {i:>2}) {p.name}{c('   ' + note, '2') if note else ''}")
    say(c("   e) Endpoint sendiri (Ollama, LM Studio, vLLM, …)    0) Lewati", "2"))
    say(c("   atau ketik nama penyedia lain untuk mencari (ada ±230)", "2"))
    while True:
        ans = ask("Pilih", "1" if popular else "e").strip()
        if ans == "0":
            return None
        if ans.lower() == "e":
            return "custom"
        if ans.isdigit() and 1 <= int(ans) <= len(popular):
            return popular[int(ans) - 1]
        if ans and cat:
            found = cat.search_providers(ans)
            if not found:
                say(f"Tak ada penyedia '{ans}'. {catalog_mod.HIDDEN_NOTE}")
            elif len(found) == 1:
                return found[0]
            else:
                i = choose("Penyedia", [f"{p.name} ({p.id})" for p in found[:30]])
                if i is not None:
                    return found[i]


def _pick_model(cat, prov):
    """Daftar model dengan pencarian: angka = pilih, kata = cari, g = $0 saja, h/t/k = urut."""
    query, free, sort, page = "", False, "baru", 0
    while True:
        items = cat.models(prov.id, query, free, sort)
        pages = max(1, -(-len(items) // PAGE))
        page = min(page, pages - 1)
        view = items[page * PAGE:(page + 1) * PAGE]
        say()
        say(c(f"{prov.name}: {len(items)} model · urut {catalog_mod.SORTS[sort]}"
              + (" · hanya $0" if free else "") + (f" · cari '{query}'" if query else "")
              + f" · hal {page + 1}/{pages}", "1"))
        if not view:
            say("  (tak ada yang cocok)")
        for i, m in enumerate(view, page * PAGE + 1):
            say(f"  {i:>3}) {m.label()}")
        say(c("  harga = $ per 1 juta token (input/output) · 👁 bisa lihat gambar · $0 menurut katalog; "
              "syarat tier gratis ditentukan penyedia", "2"))
        ans = ask("Nomor | kata = cari | g = $0 saja | h/t/k = urut harga/terbaru/konteks | m = lagi | Enter = kembali").strip()
        low = ans.lower()
        if not ans:
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(items):
            return items[int(ans) - 1]
        if low == "g":
            free, page = not free, 0
        elif low in ("h", "t", "k"):
            sort, page = {"h": "harga", "t": "baru", "k": "konteks"}[low], 0
        elif low == "m":
            page = (page + 1) % pages
        elif ans == "-":
            query, free, page = "", False, 0
        else:
            query, page = ans, 0


def _fix_loop(s: Settings, label: str, attempt, on_model=None, on_key=None) -> bool:
    """Jalankan attempt() sampai lolos; kalau gagal jelaskan dan ulang HANYA bagian yang salah.
    attempt() → (ok, pesan). on_key/on_model() → True kalau bagian itu sudah diperbaiki."""
    while True:
        say("Mengetes model…")
        res, msg = attempt()
        if res:
            ok(msg)
            return True
        prob = S.explain_model_error(msg, "")
        bad(prob.hint)
        say(c("  detail: " + " ".join(msg.split())[:220], "2"))
        say(c("  (Enter kosong = kembali ke daftar penyedia)", "2"))
        if prob.field == "key" and on_key and on_key(label):
            continue
        if prob.field in ("model", "tools") and on_model and on_model():
            continue
        return False


def _setup_catalog(s: Settings, cat, prov) -> bool:
    state = {"key": ""}
    if note := catalog_mod.NOTES.get(prov.id):
        say(c(note, "33"))
    if prov.key_required:
        have = bool(s.key(prov.key_env))
        if have:
            say(f"Key {prov.key_env} sudah tersimpan dan dipakai ulang.")
        if not have or yes("Ganti key?", False):
            say(f"Ambil key di: {c(catalog_mod.KEY_URL.get(prov.id, 'situs ' + prov.name), '4')}")
            state["key"] = ask_secret(f"Tempel {prov.key_env}")
            if not state["key"] and not have:
                return False
    holder = {"model": _pick_model(cat, prov)}
    if not holder["model"]:
        return False

    def attempt():
        return run(S.add_catalog_model(s, prov, holder["model"], key=state["key"]))

    def on_key(label):
        new = ask_secret(f"Tempel ulang key {prov.name}")
        state["key"] = new
        return bool(new)

    def on_model():
        holder["model"] = _pick_model(cat, prov)
        return bool(holder["model"])

    return _fix_loop(s, prov.name, attempt, on_model, on_key)


def _setup_custom(s: Settings) -> bool:
    base = ask("URL API (OpenAI-compatible), mis. http://localhost:11434/v1")
    model = ask("Nama model")
    key = ask_secret("API key (kosongkan kalau tak perlu)")
    if not base or not model:
        return False
    st = {"base": base, "model": model, "key": key}

    def attempt():
        return run(S.add_custom_model(s, st["model"], st["base"], key=st["key"]))

    def on_key(_):
        st["key"] = ask_secret("Tempel ulang API key")
        return bool(st["key"])

    def on_model():
        st["model"] = ask("Nama model yang benar", "")
        return bool(st["model"])

    # URL salah / tak terjangkau juga bisa diperbaiki tanpa mengulang semuanya
    def run_attempt():
        while True:
            res, msg = attempt()
            if res or S.explain_model_error(msg).field != "url":
                return res, msg
            bad(S.explain_model_error(msg).hint)
            new = ask("URL API", st["base"])
            if not new:
                return False, msg
            st["base"] = new

    return _fix_loop(s, "endpoint", run_attempt, on_model, on_key)


def step_model(s: Settings) -> bool:
    head("Model AI")
    models = (s.get("models.list") or {})
    if models:
        say("Sudah ada: " + ", ".join(models))
        if not yes("Tambah model lain?", False):
            return True
    say("Wajib minimal satu, supaya bot bisa menjawab. Pilih penyedia (dari katalog models.dev, dipakai juga oleh OpenCode).")
    cat = _catalog(s)
    while True:
        prov = _pick_provider(cat)
        if prov is None:
            s.skip("model")
            say(c("⏸ dilewati — bot belum bisa menjawab sampai model diisi.", "33"))
            return False
        good = _setup_custom(s) if prov == "custom" else _setup_catalog(s, cat, prov)
        if good and not yes("Tambah model cadangan (dipakai kalau yang utama error/limit)?", False):
            return True


def step_shell(s: Settings) -> None:
    head("Shell")
    if not os.environ.get("SANDBOX_URL"):
        say("Shell butuh mode Docker (sandbox terpisah). Mode ini tanpa Docker → dilewati.")
        s.skip("shell")
        return
    on = yes("Izinkan agent menjalankan perintah di sandbox? (perintah berisiko tetap minta tombol)")
    s.set("shell.enabled", on)
    s.skip("shell", not on)
    ok("Shell " + ("aktif" if on else "mati"))


def step_youtube(s: Settings) -> None:
    head("YouTube (opsional)")
    say("YouTube sering menolak IP server. Kalau punya cookies.txt dari browser, masukkan path-nya.")
    say("Bisa juga nanti: kirim file cookies.txt lewat Telegram /setup.")
    path = ask("Path cookies.txt (Enter = lewati)")
    if not path:
        s.skip("youtube")
        return
    src = Path(path).expanduser()
    if not src.exists():
        bad("File tak ada.")
        return
    dst = s.dir / "cookies.txt"
    dst.write_bytes(src.read_bytes())
    dst.chmod(0o600)
    s.set("media.ytdlp_cookies", str(dst))
    s.skip("youtube", False)
    ok("Cookies terpasang.")


def step_zona(s: Settings) -> None:
    head("Zona waktu")
    while True:
        tz = ask("Zona waktu", s.get("timezone") or "Asia/Jakarta")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            bad("Tak dikenal. Contoh: Asia/Jakarta, Asia/Makassar, Asia/Jayapura")
            continue
        s.set("timezone", tz)
        return


def step_discord(s: Settings) -> None:
    if step_key(s, "discord") and not s.owners("discord"):
        say(f"Setelah bot jalan, DM bot Discord-mu: {c('!pair ' + s.pairing_code(), '1')}")


def step_telegram(s: Settings) -> bool:
    if not step_key(s, "telegram"):
        return False
    step_pair_telegram(s)
    return True


def step_web(s: Settings) -> None:
    head("Web di server (opsional)")
    say("Agent bisa memeriksa & memperbaiki web di server ini lewat SSH ke user khusus 'inspire'.")
    say("Baca langsung jalan; setiap perubahan menampilkan diff dan menunggu tombolmu.")
    sites = list(s.get("host.sites") or [])
    if sites:
        say("Terdaftar: " + ", ".join(f"{x['name']} ({x['path']})" for x in sites))
    if not yes("Tambah / ubah situs?", not sites):
        if not sites:
            s.skip("web")
        return
    while True:
        path = ask("Path folder situs di server (mis. /var/www/perizinan, Enter = selesai)")
        if not path:
            break
        if not path.startswith("/"):
            bad("Harus path absolut.")
            continue
        name = ask("Nama singkat", Path(path).name.lower().replace(" ", "-"))
        sites = [x for x in sites if x["name"] != name] + [{"name": name, "path": path.rstrip("/"), "owner": ""}]
    if not sites:
        s.skip("web")
        return
    ssh_dir = s.dir / "ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    key = ssh_dir / "id_ed25519"
    if not key.exists():
        import subprocess

        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "inspire-agent", "-f", str(key)],
                       check=True)
        ok("Key SSH agent dibuat (data/ssh/id_ed25519).")
    (ssh_dir / "sites.txt").write_text("".join(f"{x['name']}|{x['path']}\n" for x in sites))
    address = s.get("host.address") or (
        "host.docker.internal" if os.environ.get("INSPIRE_MODE") == "docker" else "127.0.0.1")
    s.set("host", {**(s.get("host") or {}), "address": address, "port": int(s.get("host.port") or 22),
                   "user": "inspire", "key": "ssh/id_ed25519", "sites": sites})
    s.skip("web", False)
    ok(f"{len(sites)} situs dicatat.")
    if not os.environ.get("INSPIRE_MODE"):
        say("Langkah sudo (buat user 'inspire', sudoers) dijalankan oleh perintah server:")
        say(f"  sudo bash bin/setup-web.sh {ssh_dir}   lalu   inspire setup web-test")


def step_web_test(s: Settings) -> None:
    """Dipanggil setelah setup-web.sh: catat pemilik tiap situs, uji SSH."""
    head("Uji akses web")
    owners_file = s.dir / "ssh" / "owners.txt"
    owners = dict(line.split("|", 1) for line in owners_file.read_text().split() if "|" in line) \
        if owners_file.exists() else {}
    sites = [{**x, "owner": owners[x["name"]].strip()} for x in (s.get("host.sites") or []) if x["name"] in owners]
    s.set("host.sites", sites)
    if not sites:
        bad("Tak ada situs yang bisa dipakai (lihat pesan di atas).")
        return
    from ..config import from_settings
    from ..tools.host import Host

    cfg = from_settings(s)
    host = Host(cfg.host_address, cfg.host_port, cfg.host_user, cfg.host_key,
                str(Path(cfg.host_key).parent / "known_hosts"))
    for site in sites:
        script = host.as_owner(site["owner"], f"cd {site['path']} && test -r . && echo ok && "
                                              "(grep -E '^DB_CONNECTION=' .env 2>/dev/null || true)")
        code, out, err = run(host.run(script, timeout=20))
        if code == 0 and out.startswith("ok"):
            db = out.splitlines()[1].split("=", 1)[1] if len(out.splitlines()) > 1 else "tanpa DB"
            ok(f"{site['name']}: SSH ✓, bekerja sebagai {site['owner']}, database: {db}")
        else:
            bad(f"{site['name']}: {(err or out).strip()[:200]}")
            say("    Cek: sshd jalan? firewall mengizinkan dari Docker? AllowUsers di sshd_config?")


STEPS = {
    "telegram": step_telegram,
    "pair": step_pair_telegram,
    "model": step_model,
    "suara": lambda s: step_key(s, "suara"),
    "gambar": lambda s: step_key(s, "gambar"),
    "discord": step_discord,
    "shell": step_shell,
    "youtube": step_youtube,
    "zona": step_zona,
    "web": step_web,
    "web-test": step_web_test,
}


def wizard(s: Settings, section: str | None = None) -> None:
    try:
        if section:
            if section not in STEPS:
                bad(f"Bagian tak dikenal. Pilih: {', '.join(STEPS)}")
                return
            STEPS[section](s)
        else:
            say(c("inspire setup", "1;36") + c("  — Enter = lewati, semua bisa diisi nanti", "2"))
            mode = choose("Mode", [
                "Cepat — Telegram + pairing + 1 model (±2 menit)",
                "Lengkap — semua fitur (suara, gambar, Discord, shell, YouTube, zona)",
            ], allow_skip=False)
            if not STEPS["telegram"](s) and mode == 0:
                say("Tanpa Telegram, bisa pakai Discord.")
                step_discord(s)
            step_model(s)
            if mode == 1:
                for sid in ("suara", "gambar", "discord", "shell", "youtube", "zona"):
                    STEPS[sid](s)
    except KeyboardInterrupt:
        say()
        say(c("Dihentikan. Yang sudah diisi tetap tersimpan.", "33"))
    summary(s)


def summary(s: Settings) -> None:
    head("Status")
    say(S.render(run(S.status(s, live=True))))
    say()
    if not s.get("host.sites"):
        say("Mau agent bisa memperbaiki web di server ini? " + c("inspire setup web", "1"))
    if os.environ.get("INSPIRE_MODE") in ("docker", "native"):
        say("Ubah kapan saja: " + c("inspire setup <bagian>", "1") + " atau " + c("/setup", "1") + " di Telegram.")
    else:
        say("Jalankan bot: " + c("inspire run", "1") + "   Ubah setelan: " + c("inspire setup <bagian>", "1"))


def doctor(s: Settings) -> int:
    """Status + tes sungguhan semua key yang terisi. Return 1 kalau ada error."""
    head("Status fitur")
    feats = run(S.status(s, live=True))
    say(S.render(feats))
    head("Tes key")
    tests = []
    for sid, sec in S.KEY_SECTIONS.items():
        if s.key(sec.key):
            tests.append((sec.title, sec.check(s.key(sec.key))))
    from ..config import from_settings

    cfg = from_settings(s)
    for name, m in cfg.models.items():
        key = m.api_key if m.api_key and m.api_key != "none" else None
        tests.append((f"Model {name}{' (utama)' if name == cfg.default_model else ''}",
                      checks.model({"model": m.model, "api_base": m.api_base, "api_key": key})))

    async def all_tests():
        return await asyncio.gather(*(t for _, t in tests))

    failed = False
    for (title, _), (good, msg) in zip(tests, run(all_tests()) if tests else []):
        (ok if good else bad)(f"{title}: {msg}")
        failed |= not good
    if not tests:
        say("(belum ada key)")
    return 1 if failed or any(f.state == "err" for f in feats) else 0
