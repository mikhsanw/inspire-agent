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

### Fase 4 — belajar ✅
- Skill otomatis `skills/*.md` (judul + ringkasan masuk prompt, isi dibaca saat
  perlu), folder di-git, notif tiap perubahan.
- Adapter Discord.

## Keamanan dasar
- Whitelist user ID wajib; tanpa whitelist bot menolak start.
- API key hanya dari environment (`.env`), tidak pernah masuk config/git.
- Isi web/file = data, bukan perintah (ditegaskan di system prompt).

### Fase 5 — coding & web di server ✅

| # | Hal | Putusan |
|---|---|---|
| 1 | Coding | Skill bawaan `coding` + tool `read_file`/`write_file`/`edit_file`/`list_files` di `/workspace` sandbox; tulis file tanpa tombol |
| 2 | Jalankan kode | `python`, `pytest`, `node`, `npm`, `pip`, `git` (kecuali push), `mkdir/cp/mv` di `/workspace` otomatis; sisanya ✅ + tombol "izinkan semua 30 menit" per chat, tak berlaku untuk jadwal |
| 3 | Caveman | `/caveman auto\|on\|off`, bawaan `auto` (agent memilih); kode/angka/error utuh |
| 4 | Web di server | SSH ke user `inspire` yang bekerja sebagai pemilik situs (bukan root). Baca bebas; ubah file = diff → ✅; perintah mengubah = selalu ✅ (tanpa izin 30 menit); snapshot git/file otomatis + rollback |
| 5 | Rahasia web | `.env*`, `*.pem`, `*.key`, `id_rsa*` disamarkan; nilai rahasia disamarkan di semua keluaran; satu nilai bisa dibuka lewat ✅ |
| 6 | Database | Baca-saja (SQL satu pernyataan + transaksi READ ONLY); `SHOW/DESCRIBE` bebas; `SELECT` → ✅, maks 20 baris, kolom pribadi disamarkan |

Default keamanan: key SSH di container agent (bukan sandbox), situs milik root ditolak,
jadwal tak bisa mengubah server.

### Fase 6 — mode tugas ✅

Keputusan: tugas coding di latar belakang (60 langkah / 30 menit), per proyek git di sandbox, titik simpan
commit otomatis sebelum & sesudah, `/diff`, `/batalkan` (hanya tugas terbaru, lewat tombol), `/stop`.
Tanpa GitHub (push tetap di luar cakupan). Hanya sandbox: tool server/DB/jadwal tak dipakai di mode tugas.

### Fase 7 — sistem model ✅

- **Katalog models.dev** (seperti OpenCode) menggantikan preset buatan tangan: pilih penyedia → key (dites dulu, baru
  disimpan) → model dengan pencarian, filter $0, urutan; konteks, harga, vision tercatat di setelan. Jalur API
  dihitung PER MODEL (OpenAI-compatible dan Anthropic didukung; /responses dan SDK khusus disembunyikan).
  Penyedia langganan / OAuth disembunyikan. OpenCode: hanya model berbayar (gratis terkunci ke aplikasinya).
  "Gratis" tidak disimpulkan dari katalog.
- **Fallback tangguh** (seperti Hermes: timeout per model, rantai): cooldown eksponensial untuk limit/server, blokir
  30 menit untuk key/nama model/diblokir, batas waktu per model + total rantai, status di `/model`,
  anggaran konteks per model, catatan "dijawab oleh".
- Tidak dikerjakan (sengaja): model per peran (chat/ringkas/tugas/vision) dan pemantauan biaya (`/biaya`).

### Fase 8 — OpenCode sebagai pekerja ✅

Keputusan: `/opencode` dipicu **hanya oleh user** (bukan tool agent); OpenCode asli dijalankan **di dalam sandbox**
(tanpa Docker: tidak tersedia); fondasi mode tugas dipakai ulang (proyek git, titik simpan, `/diff`, `/batalkan`, `/stop`).
Model gratis bawaan OpenCode tanpa key; key berbayar opsional dan KHUSUS (terpisah dari key lain, karena masuk sandbox).

Hasil pengamatan OpenCode 1.19 asli (bukan tebakan): stdin harus ditutup (kalau tidak, menggantung); `--format json` →
JSONL step_start / tool_use / text / step_finish / error, kode keluar 1 saat error; tanpa `--auto` izin 'ask' ditolak
otomatis; `external_directory: deny` tak membatasi bash (batas = container); `--continue` = sesi terakhir global
(maka sesi disimpan per tugas dan dilanjutkan dengan `-s`); perintah OpenCode berjalan di grup proses sendiri (maka
`/stop` mematikan seluruh pohon proses); ±600 MB RAM.
