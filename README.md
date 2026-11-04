# English Section (Odoo 20 addon)

Addon Odoo untuk membuat tes listening gaya TOEFL secara otomatis.

Sekali klik **Generate** di Odoo, sistem akan:

1. Membuat skrip listening (dialog atau monolog, sekitar 650–800 kata) dan **20 soal pilihan ganda A–D** dalam bahasa Inggris, memakai **Gemini** (`gemini-3.8-flash`).
2. Mengubah skrip menjadi audio dengan **Gemini TTS** (`gemini-3.8-flash-tts`). Dialog memakai dua suara.
3. Merender **video MP4** dengan ffmpeg: audio di awal, lalu 20 slide soal, lalu slide kunci jawaban di akhir.
4. Menyimpan skrip, soal, audio, dan video di database. Video bisa diputar langsung di form Odoo.

## Struktur

```
english_section/
├── __manifest__.py
├── models/
│   ├── listening_test.py       # english.listening.test: state, skrip, audio, video, tombol Generate
│   └── listening_question.py   # english.listening.question: 20 soal per tes
├── services/
│   ├── gemini.py               # klien REST Gemini (teks dan TTS), validasi JSON soal
│   └── render.py               # slide PIL + ffmpeg (concat dan mux audio)
├── controllers/main.py         # GET /english_section/video/<id> (login wajib, mendukung Range)
├── views/listening_views.xml   # list, form, menu
├── data/cron.xml               # cron tiap 1 menit untuk memproses antrean Generate
└── security/ir.access.csv
```

## Persyaratan

- Odoo 20 dengan modul `web`.
- Python dengan **Pillow** (sudah ikut dengan Odoo). Tidak butuh paket tambahan karena klien Gemini memakai `urllib`.
- **ffmpeg** di PATH. Cek dengan `ffmpeg -version`.
- Akses internet ke `generativelanguage.googleapis.com`.

## Instalasi

1. Letakkan folder `english_section` di salah satu `addons_path`.
2. Pasang API key Gemini (lihat bagian berikut).
3. Install modul lewat Apps, atau dari command line:

   ```
   odoo-bin -c odoo.conf -d <nama_database> -i english_section --stop-after-init
   ```

4. Buka menu **English Section → Listening Tests**.

## API key

Key dibaca dengan urutan berikut:

1. Variabel lingkungan `GEMINI_API_KEY`.
2. File `English Section/.env.local` di folder `addons` yang berisi:

   ```
   GEMINI_API_KEY=isi_key_disini
   ```

Key **tidak pernah** disimpan ke database atau ditampilkan di Odoo. Jangan commit file `.env.local`.

## Cara pakai

1. Buat record baru di **Listening Tests**. Isi nama dan topik (opsional), misalnya `biology lecture on ecosystems`.
2. Klik **Generate**. Status berubah ke *Queued*, lalu *Generating*. Proses biasanya memakan sekitar 2–3 menit.
3. Setelah status *Done*, buka tab:
   - **Video**: pemutar video MP4.
   - **Questions**: 20 soal beserta pilihan dan kunci jawaban.
   - **Script**: teks skrip listening.
4. Jika gagal, status menjadi *Error* dan pesannya tampil di form. Klik Generate lagi untuk mencoba ulang.

## Format video

| Bagian | Durasi |
|---|---|
| Intro + audio listening | panjang audio + 1 detik |
| Slide soal (20 soal) | 15 detik per soal |
| Kunci jawaban | 20 detik |

Durasi per soal dan kunci jawaban ada di `render_video()` (`services/render.py`).

## Catatan teknis

- **Generate dijalankan oleh service Odoo, bukan dari shell.** Attachment (video dan audio) disimpan di filestore dari user yang menjalankan proses. Jika Odoo berjalan sebagai service Windows (LocalSystem), pastikan generate juga dijalankan dari service itu. Attachment yang dibuat lewat shell pengguna lain tidak akan terbaca oleh service.
- Endpoint video mengirim header `Content-Security-Policy: default-src 'self'; media-src 'self'`. Tanpa ini, browser memblokir pemutaran dari halaman Odoo.
- Video dibuat dengan `-movflags +faststart` agar browser bisa membaca durasi tanpa mengunduh seluruh file.
- Model Gemini dan nama voice ada di `services/gemini.py` (`TEXT_MODEL`, `TTS_MODEL`, `SPEAKER_VOICES`).

## Batasan

- Satu tes memakan kuota API Gemini teks dan TTS setiap kali Generate ditekan.
- Generate memakan waktu beberapa menit. Cron memproses maksimal 3 tes per menit.
- Format soal selalu pilihan ganda A–D. Tidak ada penilaian otomatis untuk jawaban siswa.
- Belum ada fitur untuk halaman publik. Akses hanya untuk user Odoo yang login.
