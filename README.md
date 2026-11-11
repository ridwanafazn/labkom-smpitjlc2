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
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Notifikasi Telegram. Opsional. Dikirim di background. |
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
```

`vercel.json` mengarahkan function ke region `sin1`. File `.python-version` memakai Python 3.14.

## Test

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```
