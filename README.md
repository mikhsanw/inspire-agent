# inspire

Agent AI pribadi ala Hermes. Telegram, memori jangka panjang, model apa saja.
Rencana lengkap: [SPEC.md](SPEC.md). Status: **Fase 2 (jadwal, web, shell sandbox)**.

## Jalan

1. Buat bot di @BotFather → dapat token. Cek user ID kamu di @userinfobot.
2. Siapkan file:
   ```bash
   cp .env.example .env
   cp config.example.yaml config.yaml
   ```
   Isi `.env` (token, user ID, API key, `SANDBOX_TOKEN` dari `openssl rand -hex 24`).
   Sesuaikan nama model di `config.yaml`.
3. Jalankan:
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```
   Tanpa Docker: `uv run inspire`. Shell lalu butuh sandbox lokal
   (`SANDBOX_TOKEN=... WORKSPACE=./workspace uv run python -m inspire.sandbox_server`,
   `sandbox_url: http://127.0.0.1:8765`) — **tidak terisolasi**, hanya untuk dev.

## Perintah Telegram

- `/new` — sesi baru
- `/model` — lihat / ganti model (`/model or-free`)
- `/memory` — isi memori
- `/jobs` — jadwal aktif
- `/help`

## Contoh

- "ingatkan aku besok jam 7 minum obat" → jadwal sekali
- "tiap Senin jam 8 rangkum berita AI" → jadwal cron + web
- "cek isi folder workspace" → shell langsung jalan
- "buat file catatan.txt" → shell minta tombol ✅/❌ dulu

## Keamanan shell

- Shell jalan di container `sandbox` terpisah: user non-root, tanpa capability,
  batas RAM/CPU/proses, port tak dibuka ke host, tak melihat `.env` / `data/`.
- Perintah baca-saja (`ls`, `cat`, `grep`, …) jalan langsung. Sisanya (termasuk
  redirect `>`, `$(…)`, `curl`, `python`) minta tombol. Diam 5 menit = batal.
- Jadwal dengan `allow_shell` (tanpa tanya) wajib disetujui user saat dibuat.
- `fetch_url` menolak alamat privat/lokal (cegah intip jaringan VPS).

## Data

Semua di `data/`:
- `inspire.db` — sesi + riwayat chat (FTS5)
- `MEMORY.md`, `USER.md` — memori yang agent kurasi
- `SOUL.md` (opsional) — ganti kepribadian default

## Test

```bash
uv run pytest -q
```

## Struktur

```
src/inspire/
  agent.py        loop agent, sesi, ringkas konteks
  llm.py          litellm + fallback
  memory.py       MEMORY.md / USER.md
  store.py        SQLite + FTS5
  config.py       config.yaml + ${ENV}
  scheduler.py    APScheduler + tabel jobs
  sandbox_server.py  server exec di container sandbox
  tools/          core, schedule, web, shell
  gateway/        base + telegram (tombol konfirmasi)
```
