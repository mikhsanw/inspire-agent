# inspire

Agent AI pribadi ala Hermes. Telegram + Discord, memori jangka panjang, model apa saja.
Rencana lengkap: [SPEC.md](SPEC.md). Status: **Fase 4 selesai (skill otomatis + Discord)** — semua fase di SPEC sudah dibangun.

## Jalan

1. Gateway (minimal satu):
   - **Telegram**: buat bot di @BotFather → token. User ID kamu dari @userinfobot.
   - **Discord**: Developer Portal → New Application → Bot → token; nyalakan
     *Message Content Intent*; undang bot (scope `bot`, izin kirim pesan). User ID:
     Settings → Advanced → Developer Mode → klik kanan namamu → Copy User ID.
     Bot jawab di DM, atau di server kalau di-mention.
2. Siapkan file:
   ```bash
   cp .env.example .env
   cp config.example.yaml config.yaml
   ```
   Isi `.env` (token, user ID, API key, `SANDBOX_TOKEN` dari `openssl rand -hex 24`,
   `GROQ_API_KEY` untuk suara, `GEMINI_API_KEY` untuk deskripsi gambar).
   Sesuaikan nama model di `config.yaml`.
3. Jalankan:
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```
   Tanpa Docker: `uv run inspire`. Shell lalu butuh sandbox lokal
   (`SANDBOX_TOKEN=... WORKSPACE=./workspace uv run python -m inspire.sandbox_server`,
   `sandbox_url: http://127.0.0.1:8765`) — **tidak terisolasi**, hanya untuk dev.

## Perintah

Telegram pakai `/`, Discord pakai `!` (bisa diubah di config).

- `/new` — sesi baru
- `/model` — lihat / ganti model (`/model or-free`)
- `/memory` — isi memori
- `/jobs` — jadwal aktif
- `/skills` — daftar skill + riwayat perubahan
- `/skillundo` — batalkan perubahan skill terakhir
- `/help`

Memori, skill, dan riwayat dipakai bersama di Telegram dan Discord (satu pemilik).
Jadwal dikirim ke chat tempat ia dibuat.

## Contoh

- "ingatkan aku besok jam 7 minum obat" → jadwal sekali
- "tiap Senin jam 8 rangkum berita AI" → jadwal cron + web
- "cek isi folder workspace" → shell langsung jalan
- "buat file catatan.txt" → shell minta tombol ✅/❌ dulu
- kirim voice note → ditranskrip (Groq Whisper), transkrip ditampilkan, lalu dijawab
- kirim foto struk → model vision lihat langsung, atau dideskripsikan dulu (Gemini)
- kirim PDF/DOCX/XLSX → teks diekstrak; salinan file ada di sandbox `/workspace/inbox`
- "ringkas video ini <link>" → subtitle via yt-dlp, kalau tak ada → audio → Whisper

## Skill

Setelah berhasil tugas sulit, agent menulis resep kerjanya sendiri ke
`data/skills/<nama>.md`. Daftar skill (nama + deskripsi) masuk prompt; isinya dibaca
saat perlu. Tiap perubahan dikirim sebagai notifikasi dan di-commit ke repo git di
`data/skills/` — `/skillundo` untuk membatalkan, atau `git -C data/skills log`.

## YouTube minta login?

YouTube sering memblokir IP VPS ("Sign in to confirm you're not a bot").
Ekspor cookies dari browser (ekstensi "Get cookies.txt LOCALLY"), simpan di
`data/cookies.txt`, lalu set `media.ytdlp_cookies: data/cookies.txt`.
Pakai akun cadangan, bukan akun utama.

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
- `skills/` — skill yang agent tulis (repo git sendiri)

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
  media.py        STT, deskripsi gambar, ekstrak file, transkrip video
  skills.py       skill markdown + git
  tools/          core, schedule, web, shell, video, skill
  gateway/        base (perintah & media bersama), telegram, discord
```
