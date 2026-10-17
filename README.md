# inspire

Agent AI pribadi ala Hermes. Telegram + Discord, memori jangka panjang, model apa saja.
Rencana lengkap: [SPEC.md](SPEC.md). Status: **Fase 4 selesai (skill otomatis + Discord)** — semua fase di SPEC sudah dibangun.

## Install (satu perintah)

Di VPS Linux (Ubuntu/Debian) atau Mac (mode dev):

```bash
curl -fsSL https://raw.githubusercontent.com/GANTI_USER/inspire/main/install.sh | bash
```

Installer akan:
1. Unduh kode ke `~/inspire`.
2. Pilih mode: **Docker** kalau ada (shell sandbox aktif). Kalau belum ada, tawarkan
   pasang Docker, atau lanjut **native** dengan shell dimatikan.
3. Buka wizard `inspire setup`: **Cepat** (token Telegram → pairing → 1 model,
   ±2 menit) atau **Lengkap**. Enter = lewati; semua bisa diisi nanti.
4. Nyalakan bot sebagai service (Docker / systemd), nyala lagi otomatis setelah reboot.

### Pairing

Tak perlu cari user ID. Wizard memberi tautan `t.me/<bot>?start=<kode>`; tekan START
dan kamu jadi pemilik. Bisa juga nanti: `inspire pair` untuk lihat kode, lalu kirim
`/pair <kode>` ke bot Telegram atau `!pair <kode>` lewat DM Discord.

### Atur kapan saja

| Dari | Cara |
|---|---|
| Telegram | `/setup` — tombol per fitur: isi key, ✅ lewati, 🗑 hapus. Pesan berisi key langsung dihapus bot. `/status` untuk cek |
| Terminal | `inspire setup <bagian>` — `telegram pair model suara gambar discord shell youtube zona` · `inspire doctor` untuk cek & tes semua key |

Key langsung aktif tanpa restart. Ganti token bot / Discord / shell perlu restart: bot
akan bertanya ✅/❌ dan mengabari setelah aktif lagi.

Yang wajib cuma **satu gateway + satu model**. Pilihan model gratis: OpenRouter
(model `:free`, daftar otomatis yang mendukung tool) dan Gemini Flash. Fitur lain
mati dengan rapi kalau belum diisi, dan `/status` menunjukkan cara menyalakannya.

### Perintah server

```
inspire setup [bagian] | doctor | pair
inspire start | stop | restart | status | logs
inspire update      # git pull + build/restart
inspire run         # jalan di depan (dev)
```

### Discord (opsional)

Developer Portal → New Application → Bot → Reset Token; nyalakan *Message Content
Intent*; undang bot (scope `bot`). Isi token lewat `/setup` atau `inspire setup discord`,
lalu DM bot `!pair <kode>`. Bot jawab di DM, atau di server kalau di-mention.

### Dev tanpa installer

```bash
uv sync && uv run inspire setup && uv run inspire run
```

## Perintah

Telegram pakai `/`, Discord pakai `!` (bisa diubah di config).

- `/new` — sesi baru
- `/model` — lihat / ganti model (`/model or-free`)
- `/memory` — isi memori
- `/jobs` — jadwal aktif
- `/skills` — daftar skill + riwayat perubahan
- `/skillundo` — batalkan perubahan skill terakhir
- `/setup` — atur fitur (Telegram) · `/status` — fitur mana yang aktif
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
Ekspor cookies dari browser (ekstensi "Get cookies.txt LOCALLY"), lalu kirim file
`cookies.txt` lewat `/setup` → YouTube (atau `inspire setup youtube`).
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
- `settings.json` — semua setelan + key (izin 600). Jangan dibagikan
- `config.yaml` (opsional) — setelan lanjutan, menimpa settings.json; contoh di
  [config.example.yaml](config.example.yaml)
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
  cli.py          inspire run / setup / doctor / pair
  settings.py     data/settings.json (+ migrasi .env/config.yaml lama)
  config.py       pohon setelan → Config, ${ENV}
  setup/          bagian setup, tes key, status, wizard terminal
  scheduler.py    APScheduler + tabel jobs
  sandbox_server.py  server exec di container sandbox
  media.py        STT, deskripsi gambar, ekstrak file, transkrip video
  skills.py       skill markdown + git
  tools/          core, schedule, web, shell, video, skill
  gateway/        base (perintah, pairing, media), telegram (+ /setup), discord
bin/inspire       perintah server (Docker / native)
install.sh        installer satu baris
```
