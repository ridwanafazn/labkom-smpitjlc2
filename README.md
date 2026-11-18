# Labkom - Sistem Peminjaman

Aplikasi Flask dan SQLite untuk mencatat peminjaman Labkom 1 dan 2, dokumentasi foto, scan QR murid, dan notifikasi Telegram.

## Menjalankan

```powershell
python -m pip install -r requirements.txt
```

Salin `.env.example` menjadi `.env` jika belum ada. Isi `FLASK_SECRET_KEY` dan `ADMIN_PASSWORD`.

```powershell
python app.py
```

Buka `http://127.0.0.1:5000`.

Untuk akses dari HP di jaringan sekolah, buka `http://IP-KOMPUTER:5000`. Pastikan Windows Firewall mengizinkan Python. Kamera browser hanya aktif melalui `localhost` atau HTTPS.

## Konfigurasi

Semua konfigurasi dibaca dari environment atau file `.env`.

| Variabel | Fungsi |
|---|---|
| `FLASK_SECRET_KEY` | Wajib. Kunci session. Aplikasi menolak start jika kosong. |
| `DATABASE_URL` | Koneksi PostgreSQL atau Neon. Kosong berarti SQLite lokal. |
| `ADMIN_NAME`, `ADMIN_PASSWORD` | Akun admin. Hash password diperbarui saat startup. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Notifikasi Telegram dan tombol persetujuan. |
| `TELEGRAM_WEBHOOK_SECRET` | Secret untuk webhook tombol Telegram. |
| `HOST`, `PORT`, `THREADS` | Alamat dan jumlah thread Waitress. |
| `BEHIND_PROXY` | Set `1` jika di belakang Caddy atau Nginx. |
| `COOKIE_SECURE` | Set `1` jika sudah memakai HTTPS. |
| `MAX_UPLOAD_MB` | Batas ukuran unggahan. Default 12 MB. |
| `LOGIN_MAX_ATTEMPTS`, `LOGIN_WINDOW_SECONDS` | Rate limit login. Default 5 kali dalam 15 menit. |
| `BACKUP_KEEP` | Jumlah backup lokal yang disimpan. |
| `BACKUP_COPY_DIR` | Folder salinan backup di lokasi lain. Opsional. |
| `BACKUP_PASSWORD` | Enkripsi AES untuk ZIP backup. Opsional. |
| `LABKOM_DB_PATH`, `LABKOM_UPLOAD_DIR`, `LABKOM_BACKUP_DIR`, `LABKOM_STUDENTS_PATH`, `LABKOM_LOG_DIR` | Path kustom. Opsional. |

## Catatan produksi

- Jalankan dengan Waitress. `python app.py` sudah memakai Waitress.
- Gunakan HTTPS di depan aplikasi, misalnya Caddy atau Nginx.
- Set `COOKIE_SECURE=1` setelah HTTPS aktif.
- Guru lama yang belum punya password harus diatur ulang lewat menu Manajemen.
- `labkom.db`, `uploads_labkom`, dan `backup_labkom` harus berada di penyimpanan permanen.
- Log audit ada di `logs/labkom.log` dan dirotasi otomatis.

## Backup

Tombol Bersihkan Semua Riwayat dan Foto selalu membuat ZIP berisi `labkom.db` dan seluruh foto sebelum menghapus data. ZIP lama dipangkas sesuai `BACKUP_KEEP`. Jika `BACKUP_COPY_DIR` diisi, salinan juga dikirim ke folder tersebut. Jika `BACKUP_PASSWORD` diisi, ZIP dienkripsi AES.

## Deploy ke Vercel

Vercel tidak mendukung SQLite lokal secara permanen. Untuk produksi, gunakan PostgreSQL Neon dan set `DATABASE_URL`. Foto otomatis disimpan sebagai `BYTEA` di PostgreSQL saat `DATABASE_URL` aktif.

```powershell
npx vercel login
npx vercel --prod
```

Environment yang perlu diisi di dashboard Vercel:

```text
FLASK_SECRET_KEY
ADMIN_NAME
ADMIN_PASSWORD
DATABASE_URL
COOKIE_SECURE=1
MAX_UPLOAD_MB=4
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
TELEGRAM_WEBHOOK_SECRET
```

`vercel.json` mengarahkan function ke region `sin1`. File `.python-version` memakai Python 3.14.

Di Vercel, folder `/tmp` tidak persisten. Karena itu tombol Bersihkan data dinonaktifkan di deployment Vercel dan riwayat tidak dapat dihapus dari aplikasi. Gunakan snapshot Neon untuk pemulihan. Fitur bersihkan tetap tersedia saat aplikasi dijalankan di server sendiri dengan penyimpanan permanen.

## Persetujuan lewat Telegram

Notifikasi pengajuan memiliki tombol Setujui dan Tolak. Agar tombol bekerja, daftarkan webhook:

```python
import requests

requests.post(
    "https://api.telegram.org/botTOKEN/setWebhook",
    data={
        "url": "https://domain-anda/telegram/webhook",
        "secret_token": "SECRET",
        "allowed_updates": '["callback_query"]',
    },
)
```

Ganti TOKEN, domain, dan SECRET sesuai environment. Setelah webhook aktif, admin dapat menyetujui atau menolak pengajuan langsung dari Telegram.

Notifikasi keluar memakai outbox di database. Worker langsung dicoba di background, lalu cron GitHub Actions memproses sisa antrean setiap lima menit. Pesan yang gagal dicoba ulang dengan jeda bertahap hingga lima kali. Untuk cron, simpan nilai `TELEGRAM_WEBHOOK_SECRET` sebagai GitHub Actions secret `LABKOM_WORKER_SECRET`.

## Optimasi performa

- Pustaka QR hanya dimuat saat halaman scanner dibuka.
- URL aset statis memakai versi deploy dan cache satu tahun.
- Foto memakai cache privat satu tahun karena nama file selalu unik.
- Pengajuan mengirim pesan dan foto dalam satu panggilan Telegram.
- Foto penyelesaian dikirim paralel, bukan berurutan.
- Notifikasi Telegram masuk ke outbox dan diproses di background, sehingga response tidak menunggu Telegram.
- Workflow GitHub Actions memanggil `/healthz` dan worker outbox selama jam sekolah untuk mengurangi cold start.
- Satu koneksi database dipakai ulang selama satu request.
- Schema version membuat migrasi dan backfill tidak dijalankan ulang setiap cold start.
- Query dashboard, jadwal, guru, dan statistik sudah digabung dan memakai index yang sesuai.

## Test

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```
