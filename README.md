# Labkom — Sistem Peminjaman

Aplikasi Flask + SQLite untuk mencatat peminjaman Labkom 1 dan 2, dokumentasi foto, scan QR murid, dan notifikasi Telegram.

## Menjalankan

```powershell
python -m pip install -r requirements.txt
# Salin .env.example menjadi .env jika .env belum ada, lalu isi:
# FLASK_SECRET_KEY, ADMIN_PASSWORD
python app.py
```

Buka `http://127.0.0.1:5000`.

Untuk diakses dari HP di jaringan sekolah, buka `http://IP-KOMPUTER:5000`. Pastikan Windows Firewall mengizinkan Python. Kamera browser hanya aktif lewat `localhost` atau HTTPS.

## Konfigurasi

Semua konfigurasi dibaca dari environment atau file `.env`:

| Variabel | Fungsi |
|---|---|
| `FLASK_SECRET_KEY` | **Wajib.** Kunci session. Aplikasi menolak start jika kosong. |
| `DATABASE_URL` | Koneksi PostgreSQL/Neon. Kosong = SQLite lokal. |
| `ADMIN_NAME`, `ADMIN_PASSWORD` | Akun admin. Hash password diperbarui saat startup. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Notifikasi Telegram (opsional, dikirim di background). |
| `HOST`, `PORT`, `THREADS` | Alamat dan jumlah thread Waitress. |
| `BEHIND_PROXY` | Set `1` jika di belakang Caddy/Nginx. |
| `COOKIE_SECURE` | Set `1` jika sudah memakai HTTPS. |
| `MAX_UPLOAD_MB` | Batas ukuran unggahan (default 12 MB). |
| `LOGIN_MAX_ATTEMPTS`, `LOGIN_WINDOW_SECONDS` | Rate limit login (default 5 kali / 15 menit). |
| `BACKUP_KEEP` | Jumlah backup lokal yang disimpan. |
| `BACKUP_COPY_DIR` | Folder salinan backup di lokasi lain (opsional). |
| `BACKUP_PASSWORD` | Enkripsi AES untuk ZIP backup (opsional). |
| `LABKOM_DB_PATH`, `LABKOM_UPLOAD_DIR`, `LABKOM_BACKUP_DIR`, `LABKOM_STUDENTS_PATH`, `LABKOM_LOG_DIR` | Path kustom (opsional). |

## Catatan produksi

- Jalankan dengan Waitress (sudah menjadi default `python app.py`), bukan Flask dev server.
- Gunakan HTTPS di depan aplikasi, misalnya Caddy/Nginx, agar kamera dan data login aman.
- Set `COOKIE_SECURE=1` setelah HTTPS aktif.
- Guru lama yang belum punya password harus diatur ulang lewat menu **Data Admin** dengan memasukkan nama yang sama dan password baru.
- `labkom.db`, `uploads_labkom`, dan `backup_labkom` harus berada di penyimpanan permanen.
- Log audit ada di `logs/labkom.log` dan dirotasi otomatis.

## Backup

Tombol **Bersihkan Semua Riwayat & Foto** selalu membuat ZIP berisi `labkom.db` dan seluruh foto sebelum menghapus data. ZIP lama dipangkas sesuai `BACKUP_KEEP`. Jika `BACKUP_COPY_DIR` diisi, salinan juga dikirim ke folder tersebut. Jika `BACKUP_PASSWORD` diisi, ZIP dienkripsi AES.

## Deploy ke Vercel

Vercel tidak mendukung SQLite lokal secara permanen. Untuk produksi, gunakan PostgreSQL (Neon) dan set `DATABASE_URL`. Foto otomatis disimpan sebagai `BYTEA` di PostgreSQL saat `DATABASE_URL` aktif, sehingga tidak butuh Vercel Blob.

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
TELEGRAM_BOT_TOKEN (opsional)
TELEGRAM_CHAT_ID (opsional)
```

`vercel.json` sudah mengarahkan function ke region `sin1`. File `.python-version` memakai Python 3.14 sesuai runtime Vercel saat ini.

## Test

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```
