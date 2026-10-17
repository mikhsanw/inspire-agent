---
name: web-laravel
description: Memeriksa dan memperbaiki web (Laravel/PHP) yang live di server user lewat tool host_* dan db_query.
---

Ini web PRODUKSI. Tiap perubahan berdampak ke pengguna nyata.

## Alur debug

1. **Situs mana?** `host_sites` untuk daftar. Kalau user tak menyebut, tanya.
2. **Kumpulkan bukti dulu, jangan menebak:**
   - Error terbaru: `host_run` → `tail -n 80 storage/logs/laravel.log`
     (atau `ls -t storage/logs | head` kalau log harian).
   - Versi & status: `php artisan --version`, `php artisan about`,
     `git status`, `git log --oneline -5`.
   - Rute terkait: `php artisan route:list --path=<potongan-url>`.
   - Konfigurasi: `host_read .env` (nilai rahasia tampil ***; pakai `host_reveal`
     hanya kalau benar-benar butuh satu nilai non-rahasia).
3. **Temukan penyebab.** Baca file yang disebut stack trace dengan `host_read`
   (pakai offset baris). Cek struktur data dengan `db_query`:
   `SHOW TABLES`, `DESCRIBE <tabel>`, lalu `SELECT` seperlunya (hasil maks 20
   baris, kolom pribadi disamarkan).
4. **Usulkan perbaikan** ke user dalam 1–3 kalimat: file mana, apa yang diubah,
   kenapa. Lalu ubah dengan `host_edit` (potongan kecil, bukan menulis ulang file).
   User akan melihat diff dan menekan ✅.
5. **Terapkan efeknya** (masing-masing lewat tombol):
   - Ubah config/route/view: `php artisan optimize:clear`
   - Ubah kode PHP dan pakai OPcache: `host_service reload php-fpm`
   - Ubah migrasi: `php artisan migrate --force` (jelaskan dampaknya dulu!)
   - Ubah aset front-end: `npm run build`
6. **Verifikasi.** `curl -sS -o /dev/null -w '%{http_code}' https://<domain>/<path>`
   dan cek log lagi. Laporkan hasilnya.
7. **Kalau salah**, `host_rollback` ke snapshot yang dibuat otomatis sebelum perubahan.

## Larangan

- Jangan `migrate:fresh`, `migrate:reset`, `db:wipe`, `DROP`, `TRUNCATE`, atau
  menghapus file di `storage/` tanpa permintaan eksplisit user.
- Jangan mengubah `.env` — minta user mengubahnya sendiri.
- Jangan `composer update` di produksi (pakai `composer install --no-dev`).
- Jangan menampilkan data pribadi warga/pelanggan di chat.
