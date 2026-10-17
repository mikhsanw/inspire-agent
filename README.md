# inspire

Agent AI pribadi ala Hermes. Telegram, memori jangka panjang, model apa saja.
Rencana lengkap: [SPEC.md](SPEC.md). Status: **Fase 1 (inti)**.

## Jalan

1. Buat bot di @BotFather → dapat token. Cek user ID kamu di @userinfobot.
2. Siapkan file:
   ```bash
   cp .env.example .env
   cp config.example.yaml config.yaml
   ```
   Isi `.env` (token, user ID, API key). Sesuaikan nama model di `config.yaml`.
3. Jalankan:
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```
   Tanpa Docker: `uv run inspire`.

## Perintah Telegram

- `/new` — sesi baru
- `/model` — lihat / ganti model (`/model or-free`)
- `/memory` — isi memori
- `/help`

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
  tools/          registry + tool inti
  gateway/        base + telegram
```
