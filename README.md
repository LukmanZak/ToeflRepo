# English Section (Odoo 20 addon)

Addon Odoo untuk membuat video tes listening gaya TOEFL secara otomatis, untuk konten YouTube.

Sekali klik **Generate** di Odoo, sistem akan:

1. Membuat satu tema besar dan **rekaman pendek** (sekitar 150-250 kata, monolog atau dialog) dengan soal pilihan ganda A-D, memakai **Gemini** (`gemini-3.5-flash-lite`). Jumlah soal dipilih di form (**3 sampai 15**). Soal dibagi serata mungkin ke rekaman, maksimal 3 soal per rekaman (misalnya 3 soal = 1 rekaman, 7 soal = 3 rekaman berisi 3, 2, 2, 15 soal = 5 rekaman). Tiap soal punya kunci jawaban dan **pembahasan** singkat.
2. Mengubah tiap rekaman menjadi audio dengan **Gemini Live** (`gemini-3.8-live`). Gender tiap peran (Lecturer, Student) ditentukan Gemini, lalu suara dipilih acak sesuai gender dan dipakai sama untuk semua rekaman dalam satu hasil Generate.
3. Merender **video MP4 1920x1080, 30 fps** dari **template HTML beranimasi** (Chromium headless + ffmpeg): kartu intro, slide rekaman dengan audio, slide soal dengan **hitung mundur**, slide **pembahasan** tepat setelah tiap soal, kunci jawaban, dan outro.
4. Membuat **thumbnail YouTube** (PNG 1280x720) dari template yang sama.
5. Menyimpan tema, suara, rekaman (skrip + audio), soal beserta pembahasan, video, dan thumbnail di database. Video bisa diputar langsung di form Odoo.

## Struktur

```
.
├── __manifest__.py
├── models/
│   ├── listening_test.py       # english.listening.test: state, jumlah soal, countdown, skrip, audio, video, tombol Generate
│   ├── listening_clip.py       # english.listening.clip: judul, skrip, audio per rekaman
│   └── listening_question.py   # english.listening.question: soal, kunci, pembahasan, terhubung ke rekaman
├── services/
│   ├── gemini.py               # klien Gemini (teks via REST, suara via Live WebSocket), pembagian soal, pemilihan suara
│   ├── render.py               # menyusun slide dari template, menjalankan capture paralel, menggabung segmen
│   └── capture.py              # subproses: 1 slide HTML -> 1 segmen MP4 (Chromium screenshot per frame -> ffmpeg)
├── video_templates/            # TEMPLATE VIDEO (edit di sini)
│   ├── base.css, base.js       # warna, font, animasi umum, hook seek(t)
│   ├── intro.html, clip.html, question.html, explanation.html
│   ├── answer_key.html, outro.html, thumbnail.html
│   └── fonts/                  # Poppins (lisensi OFL, lihat fonts/OFL.txt)
├── controllers/main.py         # GET /english_section/video/<id> (login wajib, mendukung Range)
├── views/listening_views.xml   # list, form, menu
├── data/cron.xml               # cron tiap 1 menit untuk memproses antrean Generate
└── security/ir.access.csv
```

## Persyaratan

- Odoo 20 dengan modul `web`.
- Python dengan **Jinja2** dan **Pillow** (sudah ikut dengan Odoo) dan paket **websockets** (untuk Gemini Live).
- **Playwright + Chromium** untuk merender template HTML. Di Docker ini dipasang di `Dockerfile` proyek induk (`playwright install --with-deps chromium`, `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`). `compose.yaml` perlu `shm_size: "1gb"` untuk service `odoo`, karena `/dev/shm` bawaan 64MB membuat Chromium crash. Paket apt `chromium` di Ubuntu 24.04 hanya stub snap dan tidak bisa dipakai.
- **ffmpeg** di PATH. Cek dengan `ffmpeg -version`.
- Akses internet ke `generativelanguage.googleapis.com`. Render sendiri tidak memakai internet: font dibundel lokal dan semua request non-file diblokir saat render.

## Instalasi

1. Clone repo ini ke folder bernama `english_section` (nama folder harus persis ini) di salah satu `addons_path`:

   ```
   git clone https://github.com/LukmanZak/ToeflRepo.git english_section
   ```
2. Pasang API key Gemini (lihat bagian berikut).
3. Install modul lewat Apps, atau dari command line:

   ```
   odoo-bin -c odoo.conf -d <nama_database> -i english_section --stop-after-init
   ```

   Untuk memperbarui modul yang sudah terpasang, ganti `-i` dengan `-u`.

4. Buka menu **English Section → Listening Tests**.

## API key

Key dibaca dengan urutan berikut:

1. Variabel lingkungan `GEMINI_API_KEY`.
2. File `.env.local` di folder modul ini yang berisi:

   ```
   GEMINI_API_KEY=isi_key_disini
   ```

Key **tidak pernah** disimpan ke database atau ditampilkan di Odoo. `.env.local` sudah masuk `.gitignore`; jangan di-commit.

## Cara pakai

1. Buat record baru di **Listening Tests**. Isi:
   - **Name** dan **Topic** (opsional), misalnya `biology lecture on ecosystems`.
   - **Question count**: jumlah soal di video, 3 sampai 15 (default 9).
   - **Seconds per question**: lama hitung mundur tiap soal, 5 sampai 120 detik (default 25).
2. Klik **Generate**. Status berubah ke *Queued*, lalu *Generating* dengan progress bar. Lama proses bergantung pada jumlah soal (rekaman dibacakan satu per satu, lalu video dirender).
3. Setelah status *Done*, buka tab:
   - **Video**: pemutar video MP4.
   - **Questions**: soal, pilihan, kunci jawaban, dan pembahasan.
   - **Thumbnail**: gambar thumbnail YouTube (klik kanan, simpan gambar).
   - **Clips**: skrip tiap rekaman. Tab **Script** berisi seluruh skrip.
4. Jika gagal, status menjadi *Error* dan pesannya tampil di form. Klik Generate lagi untuk mencoba ulang.

Catatan tentang waktu: TOEFL asli tidak memiliki hitung mundur per soal (waktu dihitung per section). Karena itu detik per soal bisa diatur dan berfungsi sebagai pemandu tempo untuk penonton, bukan tiruan persis ujian.

## Format video

| Bagian | Durasi |
|---|---|
| Intro | 5 detik |
| Slide rekaman + audio (per rekaman) | panjang audio + 1 detik |
| Slide soal (per soal) | 2,2 detik animasi masuk + *Seconds per question* |
| Pembahasan (per soal) | otomatis dari panjang teks, 8-20 detik |
| Kunci jawaban | 20 detik |
| Outro | 6 detik |

Konstanta durasi ada di bagian atas `services/render.py`.

## Mengedit template

Semua tampilan dan kata-kata statis ada di `video_templates/*.html` dan `base.css`. Edit file HTML-nya langsung, lalu klik **Generate** lagi (template dibaca setiap render, tidak perlu restart).

- Nilai dinamis (judul, soal, opsi, pembahasan, jumlah soal) ditulis dalam kurung kurawal ganda dan diisi oleh renderer. Kata-kata lain, seperti teks intro atau ajakan subscribe di `outro.html`, bebas diubah.
- Semua gerak memakai **animasi CSS** (`@keyframes`, `animation-delay`). Jangan memakai `setTimeout`, `requestAnimationFrame`, atau transisi: renderer tidak menunggu waktu nyata, tetapi membekukan animasi pada waktu `t` lewat `window.seek(t)` (`base.js`) lalu mengambil screenshot.
- Bagian yang bukan CSS murni, seperti angka countdown di `question.html`, dihitung di `window.onSeek(t)`.
- Animasi yang berulang (equalizer di `clip.html`) harus punya periode yang membagi 2 detik, karena 2 detik pertama di-loop selama audio diputar.
- Jangan menambahkan `{{` atau `{%` di komentar HTML atau CSS, karena dibaca sebagai sintaks Jinja2.

## Catatan teknis

- **Generate dijalankan oleh service Odoo, bukan dari shell.** Attachment (video, thumbnail, dan audio) disimpan di filestore dari user yang menjalankan proses. Jika Odoo berjalan sebagai service Windows (LocalSystem), pastikan generate juga dijalankan dari service itu. Attachment yang dibuat lewat shell pengguna lain tidak akan terbaca oleh service.
- Render memakai satu subproses `capture.py` per segmen (maksimal 3 berjalan bersamaan, `MAX_WORKERS` di `render.py`). Subproses dipakai agar crash atau lonjakan memori Chromium tidak menjatuhkan worker Odoo.
- Endpoint video mengirim header `Content-Security-Policy: default-src 'self'; media-src 'self'`. Tanpa ini, browser memblokir pemutaran dari halaman Odoo.
- Video dibuat dengan `-movflags +faststart` agar browser bisa membaca durasi tanpa mengunduh seluruh file.
- Model Gemini dan nama voice ada di `services/gemini.py` (`TEXT_MODEL`, `LIVE_MODELS`, `VOICES`).

## Batasan

- Satu tes memakan kuota API Gemini teks dan Live setiap kali Generate ditekan.
- Generate memakan waktu beberapa menit. Cron memproses maksimal 3 tes per menit.
- Format soal selalu pilihan ganda A–D. Tidak ada penilaian otomatis untuk jawaban siswa.
- Video hanya landscape 16:9. Belum ada versi Shorts vertikal, bunyi tik countdown, musik latar, atau upload otomatis ke YouTube.
- Belum ada fitur untuk halaman publik. Akses hanya untuk user Odoo yang login.
