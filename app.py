import base64
import csv
import glob
import io
import json
import logging
import mimetypes
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(ROOT, ".env"))

ON_VERCEL = bool(os.environ.get("VERCEL"))
RUNTIME_DIR = "/tmp" if ON_VERCEL else ROOT
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

DB_PATH = os.environ.get("LABKOM_DB_PATH", os.path.join(RUNTIME_DIR, "labkom.db"))
STUDENTS_PATH = os.environ.get("LABKOM_STUDENTS_PATH", os.path.join(ROOT, "students.json"))
UPLOAD_DIR = os.environ.get("LABKOM_UPLOAD_DIR", os.path.join(RUNTIME_DIR, "uploads_labkom"))
BACKUP_DIR = os.environ.get("LABKOM_BACKUP_DIR", os.path.join(RUNTIME_DIR, "backup_labkom"))
BACKUP_KEEP = max(1, int(os.environ.get("BACKUP_KEEP", "10")))
BACKUP_COPY_DIR = os.environ.get("BACKUP_COPY_DIR", "").strip()
BACKUP_PASSWORD = os.environ.get("BACKUP_PASSWORD", "")
PAGE_SIZE = max(1, int(os.environ.get("PAGE_SIZE", "50")))
MAX_UPLOAD_MB = max(1, int(os.environ.get("MAX_UPLOAD_MB", "4" if ON_VERCEL else "12")))
LOGIN_MAX_ATTEMPTS = max(1, int(os.environ.get("LOGIN_MAX_ATTEMPTS", "5")))
LOGIN_WINDOW = max(60, int(os.environ.get("LOGIN_WINDOW_SECONDS", "900")))
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
WIB = timezone(timedelta(hours=7), "WIB")
LABKOM_LOCK_ID = 728199

if USE_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row

    INTEGRITY_ERRORS = (psycopg.IntegrityError,)
else:
    psycopg = None
    INTEGRITY_ERRORS = (sqlite3.IntegrityError,)
ADMIN_NAME = os.environ.get("ADMIN_NAME", "Rahma Maulidina").strip() or "Rahma Maulidina"

SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "").strip()
if not SECRET_KEY:
    raise RuntimeError(
        "FLASK_SECRET_KEY wajib diisi. Salin .env.example menjadi .env dan isi nilainya."
    )

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get(
        "COOKIE_SECURE", "1" if ON_VERCEL else "0"
    ) == "1",
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
    MAX_FORM_MEMORY_SIZE=2 * 1024 * 1024,
)
csrf = CSRFProtect(app)

LOGIN_ATTEMPTS = {}
LOGIN_LOCK = threading.Lock()

os.makedirs(UPLOAD_DIR, exist_ok=True)


def configure_logging():
    log_dir = os.environ.get("LABKOM_LOG_DIR", os.path.join(RUNTIME_DIR, "logs"))
    try:
        os.makedirs(log_dir, exist_ok=True)
        if not any(isinstance(h, RotatingFileHandler) for h in app.logger.handlers):
            handler = RotatingFileHandler(
                os.path.join(log_dir, "labkom.log"),
                maxBytes=1_000_000,
                backupCount=5,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            app.logger.addHandler(handler)
        app.logger.setLevel(logging.INFO)
    except OSError:
        app.logger.warning("Tidak dapat menulis log ke %s", log_dir)


configure_logging()


class PgConnection:
    """Adaptor kecil agar query bergaya SQLite (?) bisa jalan di PostgreSQL."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        return self._conn.execute(sql.replace("?", "%s"), params)

    def executemany(self, sql, seq):
        return self._conn.executemany(sql.replace("?", "%s"), seq)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


@contextmanager
def db(immediate=False):
    """Buka koneksi database, commit/rollback, lalu tutup secara eksplisit.

    SQLite memakai BEGIN IMMEDIATE, PostgreSQL memakai advisory transaction lock
    agar cek bentrok + insert tidak disela request lain.
    """
    if USE_POSTGRES:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=15)
        try:
            if immediate:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (LABKOM_LOCK_ID,))
            yield PgConnection(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    if immediate:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def table_columns(c, table):
    if USE_POSTGRES:
        rows = c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=?",
            (table,),
        ).fetchall()
        return {row["column_name"] for row in rows}
    return {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db():
    with db() as c:
        if not USE_POSTGRES:
            c.execute("PRAGMA journal_mode=WAL")
        id_column = "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
        c.execute(
            f"""CREATE TABLE IF NOT EXISTS loans (
            id {id_column},
            peminjam_type TEXT NOT NULL,
            nama TEXT NOT NULL,
            kelas TEXT DEFAULT '',
            jumlah_murid INTEGER DEFAULT 0,
            nis TEXT DEFAULT '',
            lab TEXT NOT NULL,
            tanggal TEXT NOT NULL,
            waktu_mulai TEXT NOT NULL,
            waktu_selesai TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Dipinjam',
            created_at TEXT NOT NULL,
            returned_at TEXT DEFAULT '',
            photo_before TEXT DEFAULT '',
            photo_after TEXT DEFAULT ''
        )"""
        )
        # Migrasi aman: menambah kolom tanpa menghapus data transaksi lama.
        cols = table_columns(c, "loans")
        if "photo_before" not in cols:
            c.execute("ALTER TABLE loans ADD COLUMN photo_before TEXT DEFAULT ''")
        if "photo_after" not in cols:
            c.execute("ALTER TABLE loans ADD COLUMN photo_after TEXT DEFAULT ''")
        if "created_by" not in cols:
            c.execute("ALTER TABLE loans ADD COLUMN created_by TEXT DEFAULT ''")
        if "completed_by" not in cols:
            c.execute("ALTER TABLE loans ADD COLUMN completed_by TEXT DEFAULT ''")
        c.execute(
            "UPDATE loans SET created_by=nama "
            "WHERE peminjam_type='Guru' AND (created_by IS NULL OR created_by='')"
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS teachers (
            nama TEXT PRIMARY KEY, aktif INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        )"""
        )
        teacher_cols = table_columns(c, "teachers")
        if "password_hash" not in teacher_cols:
            c.execute("ALTER TABLE teachers ADD COLUMN password_hash TEXT DEFAULT ''")
        if "role" not in teacher_cols:
            c.execute("ALTER TABLE teachers ADD COLUMN role TEXT NOT NULL DEFAULT 'teacher'")
        c.execute(
            """CREATE TABLE IF NOT EXISTS students (
            nis TEXT PRIMARY KEY, nama TEXT NOT NULL, kelas TEXT NOT NULL
        )"""
        )
        blob_type = "BYTEA" if USE_POSTGRES else "BLOB"
        c.execute(
            f"""CREATE TABLE IF NOT EXISTS photos (
            filename TEXT PRIMARY KEY,
            content_type TEXT NOT NULL,
            data {blob_type} NOT NULL
        )"""
        )
        # Index untuk pola query yang dipakai halaman utama dan pengecekan bentrok.
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_loans_lab_tanggal_status "
            "ON loans(lab, tanggal, status)"
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_loans_nis_status ON loans(nis, status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_loans_created_by ON loans(created_by)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_loans_status ON loans(status)")
        try:
            # Satu murid hanya boleh punya satu pinjaman aktif.
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_loans_one_active_student "
                "ON loans(nis) WHERE status='Dipinjam' AND peminjam_type='Murid'"
            )
        except Exception:
            app.logger.warning(
                "Index murid aktif tidak dibuat karena masih ada data pinjaman ganda."
            )

        # Akun admin dibuat/diperbarui dari environment. Password disimpan sebagai hash.
        admin_password = os.environ.get("ADMIN_PASSWORD", "").strip()
        admin_hash = generate_password_hash(admin_password) if admin_password else ""
        c.execute(
            """INSERT INTO teachers(nama,aktif,created_at,password_hash,role)
               VALUES(?,1,?,?,'admin')
               ON CONFLICT(nama) DO UPDATE SET aktif=1, role='admin',
               password_hash=CASE WHEN excluded.password_hash<>''
                                  THEN excluded.password_hash
                                  ELSE teachers.password_hash END""",
            (ADMIN_NAME, datetime.now(WIB).isoformat(timespec="seconds"), admin_hash),
        )

        count = c.execute("SELECT COUNT(*) n FROM students").fetchone()["n"]
        if count == 0 and os.path.exists(STUDENTS_PATH):
            try:
                with open(STUDENTS_PATH, encoding="utf-8") as f:
                    data = json.load(f)
                rows = []
                for student in data:
                    nis = str(student.get("nis", "")).strip()
                    nama = str(student.get("nama", "")).strip()
                    kelas = str(student.get("kelas", "")).strip()
                    if nis and nama and kelas:
                        rows.append((nis, nama, kelas))
                c.executemany(
                    "INSERT INTO students(nis,nama,kelas) VALUES(?,?,?) "
                    "ON CONFLICT (nis) DO NOTHING",
                    rows,
                )
            except (OSError, json.JSONDecodeError, TypeError, AttributeError) as exc:
                app.logger.warning("Gagal mengimpor students.json: %s", exc)


def photo_content_type(filename):
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def save_photo_record(filename, file_path):
    """Di PostgreSQL, simpan foto sebagai BYTEA agar tidak butuh disk permanen."""
    if not USE_POSTGRES:
        return
    with open(file_path, "rb") as f:
        data = f.read()
    with db() as c:
        c.execute(
            "INSERT INTO photos(filename,content_type,data) VALUES(?,?,?) "
            "ON CONFLICT (filename) DO UPDATE SET "
            "content_type=excluded.content_type, data=excluded.data",
            (filename, photo_content_type(filename), data),
        )
    os.remove(file_path)


def get_photo(filename):
    safe_name = os.path.basename(filename)
    if USE_POSTGRES:
        with db() as c:
            row = c.execute(
                "SELECT content_type,data FROM photos WHERE filename=?", (safe_name,)
            ).fetchone()
        if not row:
            return None
        return bytes(row["data"]), row["content_type"]
    path = os.path.join(UPLOAD_DIR, safe_name)
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read(), photo_content_type(safe_name)


def remove_uploaded_photo(filename):
    """Hapus foto yang gagal dipakai agar tidak menumpuk sebagai file yatim."""
    if not filename:
        return
    safe_name = os.path.basename(filename)
    if USE_POSTGRES:
        try:
            with db() as c:
                c.execute("DELETE FROM photos WHERE filename=?", (safe_name,))
        except Exception:
            app.logger.warning("Gagal menghapus foto dari database: %s", safe_name)
        return
    path = os.path.join(UPLOAD_DIR, safe_name)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        app.logger.warning("Gagal menghapus foto yatim: %s", filename)


def _normalize_image(path, ext):
    """Validasi isi gambar, perbaiki orientasi, lalu kecilkan agar hemat disk."""
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((1600, 1600))
            if ext in (".jpg", ".jpeg"):
                if img.mode != "RGB":
                    img = img.convert("RGB")
                fmt, options = "JPEG", {"quality": 82, "optimize": True}
            elif ext == ".png":
                if img.mode not in ("RGB", "RGBA", "L"):
                    img = img.convert("RGBA")
                fmt, options = "PNG", {"optimize": True}
            else:
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                fmt, options = "WEBP", {"quality": 82, "method": 6}
            img.load()
        img.save(path, format=fmt, **options)
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        ValueError,
    ) as exc:
        remove_uploaded_photo(os.path.basename(path))
        raise ValueError("File foto tidak valid. Gunakan JPG, PNG, atau WEBP.") from exc


def save_uploaded_photo(file_obj, prefix):
    if not file_obj or not file_obj.filename:
        return ""
    ext = os.path.splitext(secure_filename(file_obj.filename))[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError("Foto harus berformat JPG, JPEG, PNG, atau WEBP.")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    filename = f"{prefix}_{datetime.now(WIB).strftime('%Y%m%d_%H%M%S_%f')}{ext}"
    path = os.path.join(UPLOAD_DIR, filename)
    try:
        file_obj.save(path)
        _normalize_image(path, ext)
        save_photo_record(filename, path)
    except Exception:
        remove_uploaded_photo(filename)
        raise
    return filename


def save_webcam_photo(data_url, prefix):
    if not data_url:
        return ""
    filename = ""
    try:
        header, encoded = data_url.split(",", 1)
        if not header.startswith("data:image/"):
            raise ValueError()
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError()
        ext = ".png" if "image/png" in header else ".jpg"
        filename = f"{prefix}_{datetime.now(WIB).strftime('%Y%m%d_%H%M%S_%f')}{ext}"
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        path = os.path.join(UPLOAD_DIR, filename)
        with open(path, "wb") as f:
            f.write(raw)
        _normalize_image(path, ext)
        save_photo_record(filename, path)
        return filename
    except Exception:
        if filename:
            remove_uploaded_photo(filename)
        raise ValueError("Foto webcam tidak valid atau ukurannya terlalu besar.")


def telegram_notify(loan):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False, "Telegram belum dikonfigurasi. Isi TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID."
    msg = (
        "✅ Peminjaman Labkom selesai digunakan\n"
        f"Nama: {loan['nama']}\nJenis: {loan['peminjam_type']}\n"
        f"Tanggal: {loan['tanggal']}\nLab: {loan['lab']}\n"
        f"Waktu mulai: {loan['waktu_mulai']}\nEstimasi selesai: {loan['waktu_selesai']}\n"
        f"Selesai aktual: {(loan.get('returned_at') or '')[11:16] if loan.get('returned_at') else loan['waktu_selesai']}"
    )
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": msg},
            timeout=10,
        )
        r.raise_for_status()
        if not r.json().get("ok"):
            return False, "Telegram menolak pesan; periksa token dan chat ID."
        for field, caption in [
            ("photo_before", "Foto kondisi awal"),
            ("photo_after", "Foto kondisi akhir"),
        ]:
            filename = loan.get(field) or ""
            photo = get_photo(filename) if filename else None
            if photo:
                data, content_type = photo
                pr = requests.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={
                        "chat_id": chat_id,
                        "caption": f"{caption} — {loan['nama']} ({loan['lab']})",
                    },
                    files={"photo": (filename, data, content_type)},
                    timeout=90,
                )
                pr.raise_for_status()
                if not pr.json().get("ok"):
                    return False, "Pesan terkirim, tetapi Telegram gagal mengirim salah satu foto."
        return True, "Notifikasi dan foto (jika ada) dikirim ke Telegram."
    except Exception as exc:
        # Jangan log/stringify exception mentah: URL Telegram mengandung bot token.
        app.logger.warning("Telegram gagal: %s", type(exc).__name__)
        return False, "Gagal mengirim Telegram. Periksa konfigurasi bot dan koneksi server."


def send_telegram_async(loan):
    """Kirim Telegram di thread terpisah agar request tidak tertahan."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    threading.Thread(target=telegram_notify, args=(loan,), daemon=True).start()
    return True


def find_schedule_conflict(c, lab, tanggal, mulai, selesai):
    """Return one active overlapping booking for the same lab and date, if any."""
    return c.execute(
        """SELECT id, nama, lab, tanggal, waktu_mulai, waktu_selesai
           FROM loans
           WHERE lab=? AND tanggal=? AND status='Dipinjam'
             AND waktu_mulai < ? AND waktu_selesai > ?
           ORDER BY waktu_mulai LIMIT 1""",
        (lab, tanggal, selesai, mulai),
    ).fetchone()


def csv_safe(value):
    """Cegah CSV injection saat file dibuka di Excel."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _login_key(name):
    return f"{request.remote_addr or '-'}:{name.casefold()}"


def _login_limited(key):
    now = time.monotonic()
    with LOGIN_LOCK:
        attempts = [t for t in LOGIN_ATTEMPTS.get(key, []) if now - t < LOGIN_WINDOW]
        LOGIN_ATTEMPTS[key] = attempts
        return len(attempts) >= LOGIN_MAX_ATTEMPTS


def _record_login_failure(key):
    with LOGIN_LOCK:
        LOGIN_ATTEMPTS.setdefault(key, []).append(time.monotonic())


def _clear_login_failures(key):
    with LOGIN_LOCK:
        LOGIN_ATTEMPTS.pop(key, None)


@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    app.logger.warning(
        "AUDIT CSRF ditolak ip=%s path=%s", request.remote_addr or "-", request.path
    )
    flash("Sesi formulir kedaluwarsa. Silakan coba lagi.", "error")
    return redirect(url_for("index") if "user_name" in session else url_for("login"))


@app.errorhandler(413)
def handle_too_large(error):
    flash(f"Ukuran unggahan terlalu besar. Maksimal {MAX_UPLOAD_MB} MB.", "error")
    return redirect(url_for("index") if "user_name" in session else url_for("login"))


@app.before_request
def require_login():
    public = {"login", "static"}
    if request.endpoint in public:
        return None
    if "user_name" not in session:
        return redirect(url_for("login"))
    with db() as c:
        account = c.execute(
            "SELECT role FROM teachers WHERE nama=? AND aktif=1",
            (session["user_name"],),
        ).fetchone()
    if not account:
        session.clear()
        flash("Akun sudah dinonaktifkan. Silakan hubungi Admin.", "error")
        return redirect(url_for("login"))
    # Role selalu diambil dari database, bukan dari session yang bisa basi.
    session["role"] = account["role"]
    return None


@app.context_processor
def inject_user():
    return {
        "current_user": session.get("user_name", ""),
        "is_admin": session.get("role") == "admin",
    }


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=()")
    return response


@app.errorhandler(403)
def forbidden(error):
    return render_template(
        "error.html",
        code=403,
        title="Akses ditolak",
        message="Akun Anda tidak memiliki izin untuk membuka halaman ini.",
    ), 403


@app.errorhandler(404)
def not_found(error):
    return render_template(
        "error.html",
        code=404,
        title="Halaman tidak ditemukan",
        message="Alamat yang Anda buka tidak tersedia atau sudah dipindahkan.",
    ), 404


@app.errorhandler(500)
def server_error(error):
    app.logger.exception("Kesalahan server")
    return render_template(
        "error.html",
        code=500,
        title="Terjadi kesalahan",
        message="Sistem mengalami kendala. Silakan coba lagi beberapa saat lagi.",
    ), 500


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name = " ".join(request.form.get("nama", "").strip().split())
        password = request.form.get("password", "")
        key = _login_key(name)
        if _login_limited(key):
            app.logger.warning(
                "AUDIT login dibatasi user=%s ip=%s", name or "-", request.remote_addr or "-"
            )
            flash("Terlalu banyak percobaan login. Coba lagi beberapa menit.", "error")
            return redirect(url_for("login"))
        with db() as c:
            account = c.execute(
                "SELECT nama,password_hash,role FROM teachers WHERE nama=? AND aktif=1",
                (name,),
            ).fetchone()
        if (
            not account
            or not account["password_hash"]
            or not check_password_hash(account["password_hash"], password)
        ):
            _record_login_failure(key)
            app.logger.warning(
                "AUDIT login gagal user=%s ip=%s", name or "-", request.remote_addr or "-"
            )
            flash("Nama atau password salah.", "error")
            return redirect(url_for("login"))
        _clear_login_failures(key)
        session.clear()
        session["user_name"] = account["nama"]
        session["role"] = account["role"]
        app.logger.info(
            "AUDIT login sukses user=%s role=%s ip=%s",
            account["nama"],
            account["role"],
            request.remote_addr or "-",
        )
        return redirect(url_for("index"))
    with db() as c:
        teachers = c.execute(
            "SELECT nama FROM teachers WHERE aktif=1 AND role='teacher' ORDER BY nama"
        ).fetchall()
    return render_template("login.html", teachers=teachers, admin_name=ADMIN_NAME)


@app.route("/logout")
def logout():
    app.logger.info(
        "AUDIT logout user=%s ip=%s", session.get("user_name", "-"), request.remote_addr
    )
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin/teachers", methods=["POST"])
def manage_teachers():
    if session.get("role") != "admin":
        abort(403)
    name = " ".join(request.form.get("nama", "").strip().split())
    password = request.form.get("password", "")
    if not name or name.casefold() == ADMIN_NAME.casefold():
        flash("Masukkan nama guru yang valid.", "error")
    elif len(password) < 8:
        flash("Password guru minimal 8 karakter.", "error")
    else:
        with db() as c:
            # Upsert: nama yang pernah dinonaktifkan bisa diaktifkan kembali,
            # dan password dapat diatur ulang dengan memasukkan nama yang sama.
            c.execute(
                """INSERT INTO teachers(nama,aktif,created_at,password_hash,role)
                   VALUES(?,1,?,?,'teacher')
                   ON CONFLICT(nama) DO UPDATE SET
                   aktif=1, password_hash=excluded.password_hash""",
                (
                    name,
                    datetime.now(WIB).isoformat(timespec="seconds"),
                    generate_password_hash(password),
                ),
            )
        app.logger.info(
            "AUDIT simpan guru=%s oleh=%s", name, session.get("user_name")
        )
        flash(f"Akun guru {name} disimpan dan password telah diatur.", "success")
    return redirect(url_for("index"))


@app.route("/admin/teachers/<path:name>/delete", methods=["POST"])
def delete_teacher(name):
    if session.get("role") != "admin":
        abort(403)
    if name.casefold() == ADMIN_NAME.casefold():
        flash("Akun admin tidak bisa dinonaktifkan.", "error")
        return redirect(url_for("index"))
    with db() as c:
        c.execute("UPDATE teachers SET aktif=0 WHERE nama=? AND role='teacher'", (name,))
    app.logger.info("AUDIT nonaktifkan guru=%s oleh=%s", name, session.get("user_name"))
    flash("Akun guru dinonaktifkan. Riwayat peminjaman tidak dihapus.", "success")
    return redirect(url_for("index"))


@app.route("/admin/students", methods=["POST"])
def manage_students():
    if session.get("role") != "admin":
        abort(403)
    nis = request.form.get("nis", "").strip()
    nama = request.form.get("nama", "").strip()
    kelas = request.form.get("kelas", "").strip()
    if not all([nis, nama, kelas]):
        flash("Isi NIS, nama, dan kelas murid.", "error")
    else:
        with db() as c:
            c.execute(
                """INSERT INTO students(nis,nama,kelas) VALUES(?,?,?)
                   ON CONFLICT (nis) DO UPDATE SET
                   nama=excluded.nama, kelas=excluded.kelas""",
                (nis, nama, kelas),
            )
        app.logger.info("AUDIT simpan murid=%s oleh=%s", nis, session.get("user_name"))
        flash(f"Data murid {nama} disimpan.", "success")
    return redirect(url_for("index"))


@app.route("/admin/students/<nis>/delete", methods=["POST"])
def delete_student(nis):
    if session.get("role") != "admin":
        abort(403)
    with db() as c:
        active = c.execute(
            "SELECT 1 FROM loans WHERE nis=? AND status='Dipinjam' LIMIT 1", (nis,)
        ).fetchone()
        if active:
            flash(
                "Data murid tidak dihapus karena masih memiliki peminjaman aktif.",
                "error",
            )
            return redirect(url_for("index"))
        c.execute("DELETE FROM students WHERE nis=?", (nis,))
    app.logger.info("AUDIT hapus murid=%s oleh=%s", nis, session.get("user_name"))
    flash("Data murid dihapus.", "success")
    return redirect(url_for("index"))


@app.route("/")
def index():
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    with db() as c:
        if session.get("role") == "admin":
            total_loans = c.execute("SELECT COUNT(*) n FROM loans").fetchone()["n"]
        else:
            total_loans = c.execute(
                "SELECT COUNT(*) n FROM loans WHERE created_by=?", (session["user_name"],)
            ).fetchone()["n"]
        pages = max(1, (total_loans + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        offset = (page - 1) * PAGE_SIZE

        if session.get("role") == "admin":
            loans = c.execute(
                "SELECT * FROM loans ORDER BY id DESC LIMIT ? OFFSET ?",
                (PAGE_SIZE, offset),
            ).fetchall()
            active_loans = c.execute(
                "SELECT * FROM loans WHERE status='Dipinjam' ORDER BY created_at ASC"
            ).fetchall()
            schedule = c.execute(
                "SELECT lab,tanggal,waktu_mulai,waktu_selesai FROM loans "
                "WHERE status='Dipinjam' ORDER BY tanggal,waktu_mulai LIMIT 100"
            ).fetchall()
            teachers = c.execute(
                "SELECT nama FROM teachers WHERE aktif=1 AND role='teacher' ORDER BY nama"
            ).fetchall()
            students = c.execute(
                "SELECT nis,nama,kelas FROM students ORDER BY kelas, nama"
            ).fetchall()
        else:
            loans = c.execute(
                "SELECT * FROM loans WHERE created_by=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (session["user_name"], PAGE_SIZE, offset),
            ).fetchall()
            active_loans = c.execute(
                "SELECT * FROM loans WHERE status='Dipinjam' AND created_by=? "
                "ORDER BY created_at ASC",
                (session["user_name"],),
            ).fetchall()
            schedule = c.execute(
                "SELECT lab,tanggal,waktu_mulai,waktu_selesai FROM loans "
                "WHERE status='Dipinjam' ORDER BY tanggal,waktu_mulai LIMIT 100"
            ).fetchall()
            teachers = []
            students = []
        students_count = c.execute("SELECT COUNT(*) n FROM students").fetchone()["n"]
        active = c.execute("SELECT COUNT(*) n FROM loans WHERE status='Dipinjam'").fetchone()["n"]

    return render_template(
        "index.html",
        loans=loans,
        students_count=students_count,
        active=active,
        schedule=schedule,
        active_loans=active_loans,
        teachers=teachers,
        students=students,
        today=datetime.now(WIB).strftime("%Y-%m-%d"),
        page=page,
        pages=pages,
        page_size=PAGE_SIZE,
        total_loans=total_loans,
        first_item=offset + 1 if total_loans else 0,
        last_item=min(offset + PAGE_SIZE, total_loans),
    )


@app.route("/api/student/<nis>")
def student_lookup(nis):
    with db() as c:
        s = c.execute("SELECT nis,nama,kelas FROM students WHERE nis=?", (nis,)).fetchone()
    return (jsonify(dict(s)), 200) if s else (jsonify({"error": "NIS tidak ditemukan"}), 404)


@app.route("/borrow/student", methods=["POST"])
def borrow_student():
    nis = request.form.get("nis", " ").strip()
    lab = request.form.get("lab", "").strip()
    duration_raw = request.form.get("durasi", "60").strip()
    with db(immediate=True) as c:
        s = c.execute("SELECT * FROM students WHERE nis=?", (nis,)).fetchone()
        if not s:
            flash("NIS tidak ditemukan dalam data murid.", "error")
            return redirect(url_for("index"))
        if lab not in ["Labkom 1", "Labkom 2"]:
            flash("Pilih Labkom 1 atau Labkom 2.", "error")
            return redirect(url_for("index"))
        try:
            duration = int(duration_raw)
        except ValueError:
            duration = 60
        if duration not in [30, 60, 90, 120]:
            duration = 60
        # Waktu mulai dicatat oleh server, bukan dari jam yang diketik pengguna.
        now = datetime.now(WIB)
        end = now + timedelta(minutes=duration)
        tanggal = now.strftime("%Y-%m-%d")
        mulai = now.strftime("%H:%M")
        selesai = end.strftime("%H:%M")
        # Jangan izinkan murid yang sama memiliki lebih dari satu transaksi aktif.
        existing = c.execute(
            "SELECT id,lab FROM loans WHERE nis=? AND status='Dipinjam' LIMIT 1",
            (s["nis"],),
        ).fetchone()
        if existing:
            flash(
                f"Murid ini masih memiliki peminjaman aktif di {existing['lab']}. "
                "Selesaikan transaksi tersebut terlebih dahulu.",
                "error",
            )
            return redirect(url_for("index"))
        conflict = find_schedule_conflict(c, lab, tanggal, mulai, selesai)
        if conflict:
            flash(
                f"Jadwal bentrok: {lab} sudah dibooking pukul "
                f"{conflict['waktu_mulai']} sampai {conflict['waktu_selesai']} pada tanggal {tanggal}. "
                "Pilih lab atau waktu lain.",
                "error",
            )
            return redirect(url_for("index"))
        try:
            c.execute(
                """INSERT INTO loans(peminjam_type,nama,kelas,nis,lab,tanggal,waktu_mulai,waktu_selesai,status,created_at,created_by)
                  VALUES('Murid',?,?,?,?,?,?,?,'Dipinjam',?,?)""",
                (
                    s["nama"],
                    s["kelas"],
                    s["nis"],
                    lab,
                    tanggal,
                    mulai,
                    selesai,
                    now.isoformat(timespec="seconds"),
                    session["user_name"],
                ),
            )
        except INTEGRITY_ERRORS:
            flash(
                "Murid ini masih memiliki peminjaman aktif. "
                "Selesaikan transaksi tersebut terlebih dahulu.",
                "error",
            )
            return redirect(url_for("index"))
    flash(
        f"Peminjaman murid {s['nama']} tercatat. Mulai {mulai}, estimasi selesai {selesai}.",
        "success",
    )
    return redirect(url_for("index"))


@app.route("/borrow/teacher", methods=["POST"])
def borrow_teacher():
    nama = session.get("user_name", "")
    kelas = request.form.get("kelas", "").strip()
    jumlah = request.form.get("jumlah_murid", "0").strip()
    lab = request.form.get("lab", "").strip()
    tanggal = request.form.get("tanggal", "").strip()
    mulai = request.form.get("waktu_mulai", "").strip()
    selesai = request.form.get("waktu_selesai", "").strip()
    photo_before_file = request.files.get("photo_before")
    if (
        not all([nama, kelas, tanggal, mulai, selesai])
        or lab not in ["Labkom 1", "Labkom 2"]
        or selesai <= mulai
    ):
        flash("Periksa kembali seluruh isian guru dan rentang waktunya.", "error")
        return redirect(url_for("index"))
    if not photo_before_file or not photo_before_file.filename:
        flash(
            "Guru wajib mengunggah foto kondisi awal Labkom dari HP sebelum menyimpan peminjaman.",
            "error",
        )
        return redirect(url_for("index"))
    try:
        jumlah = int(jumlah)
    except (TypeError, ValueError):
        jumlah = 0
    with db(immediate=True) as c:
        # Cek bentrok dulu supaya foto tidak tersimpan sebagai file yatim.
        conflict = find_schedule_conflict(c, lab, tanggal, mulai, selesai)
        if conflict:
            flash(
                f"Jadwal bentrok: {lab} sudah dibooking pukul "
                f"{conflict['waktu_mulai']} sampai {conflict['waktu_selesai']} pada tanggal {tanggal}. "
                "Pilih waktu lain atau periksa lab lainnya.",
                "error",
            )
            return redirect(url_for("index"))
        try:
            photo_before = save_uploaded_photo(photo_before_file, "awal_guru")
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("index"))
        try:
            c.execute(
                """INSERT INTO loans(peminjam_type,nama,kelas,jumlah_murid,lab,tanggal,waktu_mulai,waktu_selesai,status,created_at,photo_before,created_by)
                  VALUES('Guru',?,?,?,?,?,?,?,'Dipinjam',?,?,?)""",
                (
                    nama,
                    kelas,
                    jumlah,
                    lab,
                    tanggal,
                    mulai,
                    selesai,
                    datetime.now(WIB).isoformat(timespec="seconds"),
                    photo_before,
                    nama,
                ),
            )
        except Exception:
            remove_uploaded_photo(photo_before)
            app.logger.exception("Gagal menyimpan peminjaman guru")
            raise
    app.logger.info(
        "AUDIT peminjaman guru=%s lab=%s tanggal=%s %s-%s", nama, lab, tanggal, mulai, selesai
    )
    flash(f"Peminjaman oleh {nama} tercatat.", "success")
    return redirect(url_for("index"))


@app.route("/return/<int:loan_id>", methods=["GET", "POST"])
def return_loan(loan_id):
    with db() as c:
        row = c.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not row:
        flash("Data peminjaman tidak ditemukan.", "error")
        return redirect(url_for("index"))
    loan = dict(row)
    if session.get("role") != "admin" and loan.get("created_by") != session.get("user_name"):
        abort(403)
    if loan["status"] == "Selesai":
        flash("Peminjaman ini sudah ditandai selesai.", "error")
        return redirect(url_for("index"))
    if request.method == "GET":
        return render_template("return.html", loan=loan)
    try:
        if loan["peminjam_type"] == "Guru":
            photo_after = save_uploaded_photo(
                request.files.get("photo_after"), f"akhir_guru_{loan_id}"
            )
            if not photo_after:
                flash(
                    "Guru wajib mengunggah foto kondisi akhir sebelum menyelesaikan peminjaman.",
                    "error",
                )
                return redirect(url_for("return_loan", loan_id=loan_id))
        else:
            photo_after = save_webcam_photo(
                request.form.get("webcam_photo_after", ""), f"akhir_murid_{loan_id}"
            )
            if not photo_after:
                flash(
                    "Petugas harus mengambil foto kondisi akhir menggunakan webcam "
                    "sebelum menyelesaikan peminjaman.",
                    "error",
                )
                return redirect(url_for("return_loan", loan_id=loan_id))
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("return_loan", loan_id=loan_id))
    actual_return = datetime.now(WIB).isoformat(timespec="seconds")
    try:
        with db() as c:
            c.execute(
                "UPDATE loans SET status='Selesai', returned_at=?, photo_after=?, completed_by=? WHERE id=?",
                (actual_return, photo_after, session.get("user_name", ""), loan_id),
            )
            row = c.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
            loan = dict(row)
    except Exception:
        remove_uploaded_photo(photo_after)
        app.logger.exception("Gagal menyelesaikan peminjaman id=%s", loan_id)
        raise
    app.logger.info(
        "AUDIT selesai loan=%s oleh=%s", loan_id, session.get("user_name", "-")
    )
    if send_telegram_async(loan):
        flash(
            "Peminjaman ditandai selesai. Notifikasi Telegram diproses di latar belakang.",
            "success",
        )
    else:
        flash(
            "Peminjaman ditandai selesai. Telegram belum dikonfigurasi; notifikasi tidak dikirim.",
            "warning",
        )
    return redirect(url_for("index"))


def _backup_files():
    return sorted(
        glob.glob(os.path.join(BACKUP_DIR, "cadangan_sebelum_bersih_*.zip")),
        key=os.path.getmtime,
        reverse=True,
    )


def _prune_backups():
    for old in _backup_files()[BACKUP_KEEP:]:
        try:
            os.remove(old)
        except OSError:
            app.logger.warning("Gagal menghapus backup lama: %s", old)


def _copy_backup_offsite(backup_path):
    if not BACKUP_COPY_DIR:
        return
    try:
        os.makedirs(BACKUP_COPY_DIR, exist_ok=True)
        shutil.copy2(backup_path, os.path.join(BACKUP_COPY_DIR, os.path.basename(backup_path)))
    except OSError:
        app.logger.warning("Gagal menyalin backup ke %s", BACKUP_COPY_DIR)


def _open_backup_archive(backup_path):
    if BACKUP_PASSWORD:
        try:
            import pyzipper
        except ImportError as exc:
            raise RuntimeError(
                "BACKUP_PASSWORD diset tetapi pyzipper belum terpasang."
            ) from exc
        archive = pyzipper.AESZipFile(
            backup_path,
            "w",
            compression=pyzipper.ZIP_DEFLATED,
            encryption=pyzipper.WZ_AES,
        )
        archive.setpassword(BACKUP_PASSWORD.encode("utf-8"))
        return archive
    return zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED)


def _write_backup_zip(backup_path, temp_db):
    upload_files = []
    if os.path.isdir(UPLOAD_DIR):
        for filename in os.listdir(UPLOAD_DIR):
            file_path = os.path.join(UPLOAD_DIR, filename)
            if os.path.isfile(file_path):
                upload_files.append((file_path, f"uploads_labkom/{filename}"))

    with _open_backup_archive(backup_path) as archive:
        archive.write(temp_db, arcname="labkom.db")
        for file_path, arcname in upload_files:
            archive.write(file_path, arcname=arcname)


def _write_pg_backup(backup_path):
    with db() as c:
        tables = {
            table: [dict(row) for row in c.execute(f"SELECT * FROM {table}").fetchall()]
            for table in ("loans", "teachers", "students")
        }
        photos = c.execute("SELECT filename,content_type,data FROM photos").fetchall()
    with _open_backup_archive(backup_path) as archive:
        archive.writestr(
            "data.json",
            json.dumps(tables, ensure_ascii=False, indent=2, default=str),
        )
        for photo in photos:
            archive.writestr(f"photos/{photo['filename']}", bytes(photo["data"]))


@app.route("/admin/clear-loans", methods=["POST"])
def clear_all_loans():
    if session.get("role") != "admin":
        abort(403)
    if request.form.get("confirmation", "").strip() != "BERSIHKAN":
        flash("Penghapusan dibatalkan. Konfirmasi tidak sesuai.", "error")
        return redirect(url_for("index"))

    # Buat cadangan database dan seluruh foto sebelum menghapus apa pun.
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(WIB).strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"cadangan_sebelum_bersih_{stamp}.zip")
    temp_db = None
    try:
        if USE_POSTGRES:
            _write_pg_backup(backup_path)
        else:
            fd, temp_db = tempfile.mkstemp(prefix="labkom_backup_", suffix=".db")
            os.close(fd)
            with sqlite3.connect(DB_PATH) as source, sqlite3.connect(temp_db) as target:
                source.backup(target)
            _write_backup_zip(backup_path, temp_db)
        _copy_backup_offsite(backup_path)
        _prune_backups()
    except Exception:
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except OSError:
                pass
        app.logger.exception("Pencadangan gagal; data tidak dihapus")
        flash("Data tidak dihapus. Pencadangan gagal.", "error")
        return redirect(url_for("index"))
    finally:
        if temp_db and os.path.exists(temp_db):
            try:
                os.remove(temp_db)
            except OSError:
                pass

    try:
        with db() as c:
            c.execute("DELETE FROM loans")
            if USE_POSTGRES:
                c.execute("DELETE FROM photos")
        if not USE_POSTGRES:
            # Folder ini khusus untuk dokumentasi foto peminjaman.
            for filename in os.listdir(UPLOAD_DIR):
                file_path = os.path.join(UPLOAD_DIR, filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)
    except Exception:
        app.logger.exception("Pembersihan data gagal setelah backup")
        flash(
            "Cadangan dibuat, tetapi pembersihan data gagal. Periksa log server.",
            "warning",
        )
        return redirect(url_for("index"))

    app.logger.info(
        "AUDIT bersihkan semua pinjaman oleh=%s backup=%s",
        session.get("user_name", "-"),
        os.path.basename(backup_path),
    )
    flash(
        "Seluruh riwayat peminjaman dan foto dokumentasi berhasil dibersihkan. "
        "Data murid dan akun guru tetap tersimpan. "
        f"Cadangan lama tersedia di backup_labkom/{os.path.basename(backup_path)}.",
        "success",
    )
    return redirect(url_for("index"))


@app.route("/uploads_labkom/<path:filename>")
def uploaded_photo(filename):
    filename = os.path.basename(filename)
    if session.get("role") != "admin":
        with db() as c:
            allowed = c.execute(
                "SELECT 1 FROM loans WHERE created_by=? AND (photo_before=? OR photo_after=?) LIMIT 1",
                (session.get("user_name", ""), filename, filename),
            ).fetchone()
        if not allowed:
            app.logger.warning(
                "AUDIT akses foto ditolak user=%s file=%s",
                session.get("user_name", "-"),
                filename,
            )
            abort(403)
    photo = get_photo(filename)
    if not photo:
        abort(404)
    data, content_type = photo
    response = Response(data, mimetype=content_type)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


@app.route("/export.csv")
def export_csv():
    if session.get("role") != "admin":
        abort(403)
    with db() as c:
        rows = c.execute("SELECT * FROM loans ORDER BY id DESC").fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(
        [
            "ID",
            "Jenis",
            "Nama",
            "Kelas",
            "Jumlah Murid",
            "NIS",
            "Lab",
            "Tanggal",
            "Mulai",
            "Selesai",
            "Status",
            "Dibuat",
            "Dikembalikan",
        ]
    )
    for r in rows:
        w.writerow(
            [
                csv_safe(r[k])
                for k in [
                    "id",
                    "peminjam_type",
                    "nama",
                    "kelas",
                    "jumlah_murid",
                    "nis",
                    "lab",
                    "tanggal",
                    "waktu_mulai",
                    "waktu_selesai",
                    "status",
                    "created_at",
                    "returned_at",
                ]
            ]
        )
    return Response(
        "\ufeff" + out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=log_peminjaman_labkom.csv"},
    )


# Inisialisasi juga berjalan saat modul diimpor (misalnya waitress-serve app:app).
# Test dapat melewati ini dengan LABKOM_SKIP_INIT=1 lalu memanggil init_db() sendiri.
if os.environ.get("LABKOM_SKIP_INIT") != "1":
    init_db()


if __name__ == "__main__":
    from waitress import serve

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    if os.environ.get("BEHIND_PROXY", "0") == "1":
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.logger.info("Menjalankan server di http://%s:%s", host, port)
    serve(app, host=host, port=port, threads=int(os.environ.get("THREADS", "4")))
