# English Section (Odoo 20 addon)

Addon Odoo untuk membuat **YouTube Shorts** latihan listening (9:16, maksimal 90 detik) secara otomatis.

Sekali klik **Generate** di Odoo, sistem akan:

1. Menulis **satu rekaman pendek** beserta soal pilihan ganda A-D dengan **Gemini** (`gemini-3.5-flash-lite`). Ada dua mode:
   - **School (SD-SMA)**: 1 soal, bahasa Inggris sederhana (CEFR A2-B1), rekaman 40-60 kata, guru dan murid.
   - **TOEFL Listening**: 2 soal, gaya TOEFL (kuliah atau percakapan kampus), rekaman 80-100 kata.
   Gemini juga menulis **hook** bahasa Indonesia untuk 2 detik pertama, **topik rekaman** (materi), dan pembahasan tiap soal dalam **Indonesia dan Inggris**.
2. Mengubah rekaman menjadi audio dengan **Gemini Live** (`gemini-3.8-live`). Gender tiap peran (Lecturer/Teacher, Student) ditentukan Gemini, suaranya dipilih acak sesuai gender. Waktu bicara tiap peran dicatat (timeline).
3. Merender **video MP4 1080x1920, 30 fps** dari template HTML beranimasi (Chromium headless + ffmpeg).
4. Menyimpan hasilnya di database. Video bisa diputar langsung di form Odoo.

## Isi video

Tanpa judul atau intro terpisah:

| Bagian | Isi |
|---|---|
| Hook (0-2,3 dtk) | Kalimat hook animasi, satu kata di-stabilo. Audio belum mulai. |
| Listening | Hook berubah jadi **MATERI** (topik rekaman). Audio mulai di detik ke-2. Ikon pembicara menyala sesuai giliran bicara, cincin dan gelombang suara mengikuti **volume audio asli**. Teks rekaman tidak ditampilkan. |
| Soal (per soal) | Soal, 4 opsi, hitung mundur (merah di 3 detik terakhir). |
| Jawaban (per soal) | Opsi benar menyala hijau, kartu "KENAPA?" dengan baris Indonesia dan Inggris. |
| Penutup (3 dtk) | Ajakan menulis skor di komentar. |

Total kira-kira 45 detik (school) dan 80 detik (toefl). Sebelum render, `render.fit_budget` memendekkan hitung mundur bila total melewati 88 detik, dan menolak render bila tetap tidak muat.

## Struktur

```
.
├── __manifest__.py
├── models/
│   ├── short.py                # english.short: mode, state, Generate, cron antrean, render dan simpan
│   └── short_question.py       # english.short.question: soal, kunci, pembahasan ID dan EN
├── services/
│   ├── gemini.py               # klien Gemini: skrip (generate_short), validasi, suara + timeline (synthesize)
│   ├── render.py               # audio_levels, hook_words, anggaran durasi, menyusun segmen, menggabung
│   └── capture.py              # subproses: 1 slide HTML -> 1 segmen MP4 (screenshot per frame -> ffmpeg)
├── video_templates/            # TEMPLATE VIDEO (edit di sini)
│   ├── base.css, base.js       # tema terang, font, animasi umum, hook seek(t)
│   ├── listening.html          # hook -> materi, ikon pembicara, gelombang suara
│   ├── question.html, answer.html, outro.html
│   └── fonts/                  # Poppins (lisensi OFL, lihat fonts/OFL.txt)
├── tests/test_short_helpers.py # tes unit murni (tanpa Odoo/Gemini)
├── controllers/main.py         # GET /english_section/short/<id> (login wajib, mendukung Range)
├── views/short_views.xml       # list, form, menu
├── data/cron.xml               # cron tiap 1 menit untuk memproses antrean Generate
└── security/ir.access.csv
```

## Persyaratan

- Odoo 20 dengan modul `web`.
- Python dengan **Jinja2** (ikut Odoo) dan paket **websockets** (untuk Gemini Live).
- **Playwright + Chromium** untuk merender template HTML. Di Docker ini dipasang di `Dockerfile` proyek induk (`PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`). `compose.yaml` perlu `shm_size: "1gb"` untuk service `odoo`, karena `/dev/shm` bawaan 64MB membuat Chromium crash.
- **ffmpeg** di PATH.
- Akses internet ke `generativelanguage.googleapis.com`. Render sendiri tidak memakai internet: font dibundel lokal dan semua request non-file diblokir saat render.

## Instalasi

1. Clone repo ini ke folder bernama `english_section` (nama folder harus persis ini) di salah satu `addons_path`.
2. Pasang API key Gemini (lihat bagian berikut).
3. Install modul lewat Apps, atau: `odoo-bin -c odoo.conf -d <db> -i english_section --stop-after-init` (ganti `-i` dengan `-u` untuk memperbarui).
4. Buka menu **English Section → Shorts**.

Memperbarui dari versi 16:9 lama: modul lama (`english.listening.*`) dihapus. Datanya tidak dimigrasi; buat cadangan database sebelum upgrade bila perlu.

## API key

Urutan pembacaan:

1. Variabel lingkungan `GEMINI_API_KEY`.
2. File `.env.local` di folder modul ini: `GEMINI_API_KEY=isi_key_disini`.

Key **tidak pernah** disimpan ke database atau ditampilkan di Odoo. `.env.local` sudah masuk `.gitignore`; jangan di-commit.

## Cara pakai

1. Buat record baru di **Shorts**. Isi **Name**, **Mode** (School atau TOEFL), **Topic** (opsional, petunjuk untuk Gemini), dan **Seconds per question** (default 10, 5 sampai 20).
2. Klik **Generate**. Status berubah ke *Queued*, lalu *Generating* dengan progress bar.
3. Setelah *Done*: tab **Video** (pemutar), **Questions** (soal, kunci, pembahasan), **Script** (skrip rekaman). Hook dan topik tampil di bagian atas form.
4. Jika gagal, status *Error* dan pesannya tampil di form. Klik Generate lagi untuk mencoba ulang.

## Mengedit template

Semua tampilan dan kata statis ada di `video_templates/*.html` dan `base.css`. Edit langsung lalu Generate lagi (template dibaca setiap render).

- Nilai dinamis ditulis dalam kurung kurawal ganda dan diisi renderer. Kata statis (misalnya "KENAPA?", "MATERI", teks penutup) bebas diubah.
- Semua gerak memakai **animasi CSS**. Jangan memakai `setTimeout`, `requestAnimationFrame`, atau transisi: renderer membekukan animasi pada waktu `t` lewat `window.seek(t)` (`base.js`) lalu mengambil screenshot.
- Bagian yang bukan CSS murni (hitung mundur, bar suara, ikon yang menyala) dihitung di `window.onSeek(t)`.
- Jangan menambahkan `{{` atau `{%` di komentar HTML atau CSS, karena dibaca sebagai sintaks Jinja2.
- Safe zone Shorts: biarkan ~250 px bawah dan ~110 px kanan bebas dari teks penting (tertutup tombol dan judul YouTube).

## Tes

Tes unit murni (tanpa Odoo, Gemini, atau Chromium), dari folder modul:

```
python -m unittest discover -s tests -v
```

## Catatan teknis

- **Generate dijalankan oleh service Odoo (cron), bukan dari shell.** Attachment (video, audio) disimpan di filestore dari user yang menjalankan proses.
- Render memakai satu subproses `capture.py` per segmen (maksimal 3 bersamaan, `MAX_WORKERS` di `render.py`).
- Endpoint video mengirim `Content-Security-Policy: default-src 'self'; media-src 'self'` dan mendukung Range, agar bisa diputar dan di-seek di halaman Odoo.
- Audio dimulai `AUDIO_DELAY` (2 detik) setelah video mulai lewat filter `adelay` di `capture.py`.
- Model Gemini dan nama voice ada di `services/gemini.py` (`TEXT_MODEL`, `LIVE_MODELS`, `VOICES`).

## Batasan

- Setiap Generate memakai kuota Gemini teks dan Live.
- Format soal selalu pilihan ganda A-D, tanpa penilaian otomatis.
- Hanya vertikal 9:16 (Shorts). Belum ada bunyi tik countdown, musik latar, atau upload otomatis ke YouTube.
- Belum ada halaman publik. Akses hanya untuk user Odoo yang login.
