---
name: coding
description: Menulis, menjalankan, dan memperbaiki kode di sandbox /workspace (skrip, aplikasi kecil, tes).
---

## Alur

1. **Pahami dulu.** Ulangi tujuan dalam 1–2 kalimat. Kalau ada yang ambigu dan
   menentukan desain (bahasa, input/output, di mana dipakai), tanya SATU hal dulu.
2. **Tempat kerja.** Satu proyek = satu folder: `/workspace/<nama-proyek>/`.
   Cek isi dulu dengan `list_files` kalau proyek sudah ada.
3. **Tulis** dengan `write_file` (file baru) / `edit_file` (ubah sebagian).
   Jangan pakai `echo > file` di shell. Buat kecil dulu yang bisa jalan.
4. **Jalankan** dengan `shell`, dari folder proyek:
   `cd /workspace/<proyek> && python main.py` — atau `pytest -q`, `node index.js`,
   `npm test`. Perintah coding umum ini jalan tanpa tombol.
5. **Baca error sampai habis**, perbaiki penyebabnya (bukan gejalanya), jalankan
   lagi. Ulangi sampai lulus. Maksimal ~5 putaran; kalau masih gagal, laporkan
   apa yang sudah dicoba dan dugaanmu.
6. **Tes.** Untuk logika yang tidak sepele, tulis tes kecil (`pytest`) dan jalankan.
7. **Serahkan.** Jelaskan singkat: apa yang dibuat, cara menjalankan, batasannya.
   Kirim file hasil dengan `send_file` kalau user butuh filenya. Untuk banyak file:
   `cd /workspace && zip -r proyek.zip proyek` lalu `send_file`.

## Pekerjaan besar

Kalau pekerjaan jelas butuh banyak file atau lebih dari ~10 langkah (aplikasi kecil, refactor,
beberapa fitur), sarankan user memakai `/tugas <deskripsi>`: berjalan di latar belakang dengan
batas 60 langkah, titik simpan git otomatis, `/diff` untuk melihat hasil, dan `/batalkan` untuk
mengembalikan semuanya. `/proyek baru <nama>` memilih folder proyeknya.

## Kebiasaan

- Dependensi: `pip install --user <paket>` / `npm install` di folder proyek.
  Catat di `requirements.txt` / `package.json`.
- `git init` di folder proyek untuk pekerjaan yang lebih dari satu file; commit
  setiap kali sesuatu jalan, supaya bisa mundur.
- Jangan menulis rahasia (token, password) ke file. Minta user mengisinya sendiri.
- Sandbox tak punya port terbuka ke luar: server web di sini hanya bisa dites
  dengan `curl localhost:<port>` dari sandbox itu sendiri.
- Kode untuk web LIVE di server user bukan di sini — pakai tool `host_*`
  (lihat skill `web-laravel`).
