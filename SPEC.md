# inspire — SPEC

Agent AI pribadi ala Hermes, dibangun sendiri. Satu user, jalan 24 jam di VPS,
ngobrol lewat Telegram (Discord nanti), ingat jangka panjang, bisa kirim pesan
duluan (jadwal), dan bisa pakai model apa saja lewat `litellm`.

## Keputusan

| # | Hal | Putusan |
|---|---|---|
| 1 | User | Satu user (pemilik). Bot hanya jawab Telegram user ID di whitelist |
| 2 | Bahasa | Python 3.12 + `litellm` |
| 3 | Tempat jalan | VPS, Docker. Dev di laptop pakai compose yang sama |
| 4 | Gateway | Telegram dulu. Lapisan `Gateway` abstrak, Discord = adapter baru |
| 5 | Memori | `MEMORY.md` + `USER.md` (dibatasi karakter) + riwayat chat SQLite FTS5 |
| 6 | Pengingat | Ingat + jadwal cron (APScheduler), agent bisa kirim duluan |
| 7 | Model | Config + `/model` + fallback otomatis. DeepSeek berbayar utama, free (OpenRouter `:free`, OpenCode Zen) cadangan |
| 8 | Alat | memory, search_history, schedule, web_search/fetch_url (`ddgs`), shell |
| 9 | Shell | Sandbox Docker + tombol ✅/❌ Telegram untuk perintah berbahaya |
| 10 | Media | Ubah ke teks default; kirim langsung kalau model aktif support vision |
| 11 | Helper | Groq Whisper (suara), Gemini / OpenRouter free (gambar). Media dikirim ke API luar |
| 12 | Skill | Agent tulis `skills/*.md` sendiri. Notif Telegram + git untuk rollback |
| 13 | Konteks | Ringkas otomatis saat hampir penuh + `/new` + sesi baru setelah diam ~6 jam |
| 14 | Pondasi | Dari nol, loop tipis. Hermes hanya contekan, bukan dependensi |
| 15 | Lokasi | `~/Projects/inspire`, repo git sendiri |

## Fase

### Fase 1 — inti ✅
- Loop agent: pesan → model → tool call → jalankan → ulang (batas iterasi).
- `litellm` router: daftar model di config, fallback berurutan saat error/limit,
  perintah `/model` untuk ganti model aktif tanpa restart.
- Telegram (teks): `/start`, `/new`, `/model`, `/memory`, whitelist user ID.
- SQLite: `sessions`, `messages`, `messages_fts` (FTS5) + tool `search_history`.
- Memori: `MEMORY.md` + `USER.md`, entri dipisah `§`, batas karakter.
  Tool `memory` (add / replace / remove). Snapshot dibekukan per sesi
  (prompt stabil → prefix cache model murah).
- Konteks: ringkas pesan lama saat estimasi token > ambang; sesi baru otomatis
  setelah diam `idle_hours`.
- Docker + compose, volume `data/`.

### Fase 2 — hidup ✅
- Tool `schedule` (buat / list / hapus job), tabel `jobs`, APScheduler.
  Job jalan = agent dijalankan dengan prompt job, hasil dikirim ke Telegram.
- `web_search` (`ddgs`) + `fetch_url` (ambil teks halaman).
- `shell` di container sandbox. Daftar perintah aman jalan langsung; sisanya
  minta tombol ✅/❌. Job bisa ditandai "boleh tanpa tanya".

### Fase 3 — indra ✅
- Voice note → Groq Whisper → teks.
- Gambar → kirim langsung kalau model vision, kalau tidak → deskripsi via helper.
- File (PDF/DOCX/XLSX) → ekstrak teks.
- `video_url` → `yt-dlp` subtitle, kalau tak ada → audio → Whisper.
- Semua hasil teks disimpan di riwayat (bisa dicari).

### Fase 4 — belajar
- Skill otomatis `skills/*.md` (judul + ringkasan masuk prompt, isi dibaca saat
  perlu), folder di-git, notif tiap perubahan.
- Adapter Discord.

## Keamanan dasar
- Whitelist user ID wajib; tanpa whitelist bot menolak start.
- API key hanya dari environment (`.env`), tidak pernah masuk config/git.
- Isi web/file = data, bukan perintah (ditegaskan di system prompt).
