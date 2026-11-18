import base64
import csv
import glob
import hmac
import io
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
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
    g,
    has_request_context,
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


def _asset_version():
    """Versi URL aset statis agar cache panjang aman saat deploy baru."""
    commit = os.environ.get("VERCEL_GIT_COMMIT_SHA", "").strip()
    if commit:
        return commit[:12]
    latest = 0
    static_dir = os.path.join(ROOT, "static")
    for dirpath, _, filenames in os.walk(static_dir):
        for filename in filenames:
            try:
                latest = max(latest, os.path.getmtime(os.path.join(dirpath, filename)))
            except OSError:
                continue
    return str(int(latest))


ASSET_VERSION = _asset_version()
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
SCHEMA_VERSION = "2"

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
    WTF_CSRF_TIME_LIMIT=8 * 60 * 60,
    SEND_FILE_MAX_AGE_DEFAULT=31536000,
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


def _new_connection():
    if USE_POSTGRES:
        return PgConnection(
            psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=15)
        )
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def db(immediate=False):
    """Buka koneksi database, commit/rollback, lalu tutup secara eksplisit.

    Dalam request, satu koneksi dipakai ulang oleh semua blok db() agar halaman
    tidak membuka koneksi Neon berkali-kali. Blok bersarang tidak commit sendiri;
    commit dilakukan oleh blok terluar. Koneksi ditutup di teardown request.

    SQLite memakai BEGIN IMMEDIATE, PostgreSQL memakai advisory transaction lock
    agar cek bentrok + insert tidak disela request lain.
    """
    request_context = has_request_context()
    conn = getattr(g, "db_conn", None) if request_context else None
    depth = getattr(g, "db_depth", 0) if request_context else 0
    outer = conn is None or depth == 0

    if conn is None:
        conn = _new_connection()
        if request_context:
            g.db_conn = conn
            g.db_depth = 0

    if outer:
        try:
            if immediate:
                if USE_POSTGRES:
                    conn.execute(
                        "SELECT pg_advisory_xact_lock(%s)", (LABKOM_LOCK_ID,)
                    )
                else:
                    conn.execute("BEGIN IMMEDIATE")
        except Exception:
            if request_context:
                g.db_conn = None
            conn.close()
            raise

    if request_context:
        g.db_depth = depth + 1
    try:
        yield conn
        if outer:
            conn.commit()
    except Exception:
        if outer:
            conn.rollback()
        raise
    finally:
        if request_context:
            g.db_depth = depth
        if outer and not request_context:
            conn.close()


@app.teardown_request
def close_request_db(exception=None):
    if not has_request_context():
        return
    conn = g.pop("db_conn", None)
    g.pop("db_depth", None)
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


def table_columns(c, table):
    if USE_POSTGRES:
        rows = c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=?",
            (table,),
        ).fetchall()
        return {row["column_name"] for row in rows}
    return {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}


def parse_classes(raw):
    parts = re.split(r"[,;\n]+", raw or "")
    classes = []
    seen = set()
    for part in parts:
        name = " ".join(part.strip().split())
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        classes.append(name)
    return classes


def _loan_ref_count(c, name):
    row = c.execute(
        "SELECT COUNT(*) n FROM loans WHERE created_by=? OR completed_by=?",
        (name, name),
    ).fetchone()
    return row["n"] if row else 0


def _ensure_teachers_schema(c):
    """Pastikan tabel guru memakai ID permanen dan nama unik tanpa beda huruf."""
    id_type = "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    c.execute(
        f"""CREATE TABLE IF NOT EXISTS teachers (
        id {id_type},
        nama TEXT NOT NULL,
        aktif INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        password_hash TEXT DEFAULT '',
        role TEXT NOT NULL DEFAULT 'teacher'
    )"""
    )
    cols = table_columns(c, "teachers")
    if "password_hash" not in cols:
        c.execute("ALTER TABLE teachers ADD COLUMN password_hash TEXT DEFAULT ''")
    if "role" not in cols:
        c.execute("ALTER TABLE teachers ADD COLUMN role TEXT NOT NULL DEFAULT 'teacher'")
    if "id" in cols:
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_teachers_name_lower "
            "ON teachers (lower(nama))"
        )
        return

    rows = [
        dict(row)
        for row in c.execute(
            "SELECT nama,aktif,created_at,password_hash,role FROM teachers"
        ).fetchall()
    ]
    groups = {}
    for row in rows:
        key = str(row["nama"]).strip().casefold()
        groups.setdefault(key, []).append(row)

    canonical_rows = []
    for group in groups.values():
        scored = [
            (
                _loan_ref_count(c, row["nama"]),
                int(row["aktif"] or 0),
                str(row["created_at"] or ""),
                row,
            )
            for row in group
        ]
        canonical = max(scored, key=lambda item: (item[0], item[1], item[2]))[3]
        for row in group:
            if row["nama"] != canonical["nama"]:
                c.execute(
                    "UPDATE loans SET created_by=? WHERE created_by=?",
                    (canonical["nama"], row["nama"]),
                )
                c.execute(
                    "UPDATE loans SET completed_by=? WHERE completed_by=?",
                    (canonical["nama"], row["nama"]),
                )
        canonical_rows.append(canonical)

    c.execute("DROP TABLE IF EXISTS teachers_new")
    c.execute(
        f"""CREATE TABLE teachers_new (
        id {id_type},
        nama TEXT NOT NULL,
        aktif INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        password_hash TEXT DEFAULT '',
        role TEXT NOT NULL DEFAULT 'teacher'
    )"""
    )
    for row in canonical_rows:
        c.execute(
            "INSERT INTO teachers_new(nama,aktif,created_at,password_hash,role) "
            "VALUES(?,?,?,?,?)",
            (
                row["nama"],
                row["aktif"],
                row["created_at"],
                row["password_hash"] or "",
                row["role"] or "teacher",
            ),
        )
    c.execute("DROP TABLE teachers")
    c.execute("ALTER TABLE teachers_new RENAME TO teachers")
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_teachers_name_lower "
        "ON teachers (lower(nama))"
    )
    app.logger.info(
        "Migrasi tabel guru selesai. %s akun diproses, %s akun disimpan.",
        len(rows),
        len(canonical_rows),
    )


def _meta_get(c, key):
    row = c.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def _meta_set(c, key, value):
    c.execute(
        "INSERT INTO app_meta(key,value) VALUES(?,?) "
        "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _admin_password_fingerprint(password):
    if not password:
        return ""
    return hmac.new(SECRET_KEY.encode(), password.encode(), "sha256").hexdigest()


def _init_schema(c):
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
    if "decision_by" not in cols:
        c.execute("ALTER TABLE loans ADD COLUMN decision_by TEXT DEFAULT ''")
    if "decision_at" not in cols:
        c.execute("ALTER TABLE loans ADD COLUMN decision_at TEXT DEFAULT ''")
    if "decision_note" not in cols:
        c.execute("ALTER TABLE loans ADD COLUMN decision_note TEXT DEFAULT ''")
    c.execute(
        "UPDATE loans SET created_by=nama "
        "WHERE peminjam_type='Guru' AND (created_by IS NULL OR created_by='')"
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS loan_classes (
        loan_id INTEGER NOT NULL,
        kelas TEXT NOT NULL,
        PRIMARY KEY (loan_id, kelas)
    )"""
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_loan_classes_loan ON loan_classes(loan_id)"
    )
    for loan in c.execute("SELECT id,kelas FROM loans").fetchall():
        for class_name in parse_classes(loan["kelas"]):
            c.execute(
                "INSERT INTO loan_classes(loan_id,kelas) VALUES(?,?) "
                "ON CONFLICT (loan_id,kelas) DO NOTHING",
                (loan["id"], class_name),
            )
    _ensure_teachers_schema(c)
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
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_loans_status_tanggal_waktu "
        "ON loans(status, tanggal, waktu_mulai)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_loans_status_created "
        "ON loans(status, created_at)"
    )
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


def _ensure_admin(c):
    admin_password = os.environ.get("ADMIN_PASSWORD", "").strip()
    fingerprint = _admin_password_fingerprint(admin_password)
    stored_fingerprint = _meta_get(c, "admin_password_fingerprint")
    admin_row = c.execute(
        "SELECT id,nama,password_hash FROM teachers WHERE lower(nama)=lower(?)",
        (ADMIN_NAME,),
    ).fetchone()

    if admin_row:
        admin_id = admin_row["id"]
        if admin_password and (
            not admin_row["password_hash"] or fingerprint != stored_fingerprint
        ):
            c.execute(
                "UPDATE teachers SET nama=?,aktif=1,role='admin',password_hash=? WHERE id=?",
                (ADMIN_NAME, generate_password_hash(admin_password), admin_id),
            )
            _meta_set(c, "admin_password_fingerprint", fingerprint)
        else:
            c.execute(
                "UPDATE teachers SET nama=?,aktif=1,role='admin' WHERE id=?",
                (ADMIN_NAME, admin_id),
            )
    else:
        admin_hash = generate_password_hash(admin_password) if admin_password else ""
        admin_id = c.execute(
            "INSERT INTO teachers(nama,aktif,created_at,password_hash,role) "
            "VALUES(?,1,?,?,'admin') RETURNING id",
            (
                ADMIN_NAME,
                datetime.now(WIB).isoformat(timespec="seconds"),
                admin_hash,
            ),
        ).fetchone()["id"]
        if admin_password:
            _meta_set(c, "admin_password_fingerprint", fingerprint)

    # Hanya satu akun admin yang berasal dari ADMIN_NAME. Jika nama admin
    # diganti, akun admin lama diturunkan menjadi guru agar tidak menyisakan
    # akses admin dengan password lama.
    c.execute(
        "UPDATE teachers SET role='teacher' WHERE role='admin' AND id<>?",
        (admin_id,),
    )


def _import_students(c):
    count = c.execute("SELECT COUNT(*) n FROM students").fetchone()["n"]
    if count != 0 or not os.path.exists(STUDENTS_PATH):
        return
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


def init_db():
    # Di PostgreSQL, serialisasi inisialisasi antar-instance Vercel agar
    # migrasi dan pembuatan akun admin tidak saling menabrak saat cold start.
    with db(immediate=USE_POSTGRES) as c:
        if not USE_POSTGRES:
            c.execute("PRAGMA journal_mode=WAL")
        c.execute(
            "CREATE TABLE IF NOT EXISTS app_meta "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        if _meta_get(c, "schema_version") != SCHEMA_VERSION:
            _init_schema(c)
            _meta_set(c, "schema_version", SCHEMA_VERSION)
        _ensure_admin(c)
        _import_students(c)


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


def telegram_configured():
    return bool(
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        and os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    )


def telegram_send_photo(filename, data, content_type, caption, reply_markup=None):
    """Kirim satu foto dengan caption. Dipakai agar pengajuan hanya satu panggilan API."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False, "Telegram belum dikonfigurasi."
    try:
        payload = {"chat_id": chat_id, "caption": str(caption or "")[:1024]}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data=payload,
            files={"photo": (filename, data, content_type)},
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            return False, "Telegram menolak foto."
        app.logger.info(
            "Telegram foto terkirim message_id=%s",
            result.get("result", {}).get("message_id"),
        )
        return True, "Foto Telegram terkirim."
    except Exception as exc:
        app.logger.warning("Telegram foto gagal: %s", type(exc).__name__)
        return False, "Gagal mengirim foto Telegram."


def telegram_send(message, photo_fields=None, reply_markup=None):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False, "Telegram belum dikonfigurasi."
    try:
        payload = {"chat_id": chat_id, "text": message}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            return False, "Telegram menolak pesan."
        app.logger.info(
            "Telegram pesan terkirim message_id=%s",
            payload.get("result", {}).get("message_id"),
        )
        for field, caption in photo_fields or []:
            filename = field or ""
            photo = get_photo(filename) if filename else None
            if not photo:
                continue
            data, content_type = photo
            photo_response = requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": (filename, data, content_type)},
                timeout=20,
            )
            photo_response.raise_for_status()
            photo_payload = photo_response.json()
            if not photo_payload.get("ok"):
                return False, "Pesan terkirim, tetapi salah satu foto gagal dikirim."
            app.logger.info(
                "Telegram foto terkirim message_id=%s",
                photo_payload.get("result", {}).get("message_id"),
            )
        return True, "Notifikasi Telegram terkirim."
    except Exception as exc:
        app.logger.warning("Telegram gagal: %s", type(exc).__name__)
        return False, "Gagal mengirim Telegram. Periksa konfigurasi bot."


def telegram_notify(loan):
    message = (
        "Peminjaman Labkom selesai digunakan\n"
        f"Nama: {loan['nama']}\nJenis: {loan['peminjam_type']}\n"
        f"Tanggal: {loan['tanggal']}\nLab: {loan['lab']}\n"
        f"Waktu mulai: {loan['waktu_mulai']}\nEstimasi selesai: {loan['waktu_selesai']}\n"
        f"Selesai aktual: {(loan.get('returned_at') or '')[11:16] if loan.get('returned_at') else loan['waktu_selesai']}"
    )
    ok, status = telegram_send(message)
    if not ok:
        return False, status

    photos = []
    for field, caption in (
        (loan.get("photo_before") or "", "Foto kondisi awal"),
        (loan.get("photo_after") or "", "Foto kondisi akhir"),
    ):
        photo = get_photo(field) if field else None
        if photo:
            data, content_type = photo
            photos.append((field, data, content_type, caption))
    if not photos:
        return True, "Notifikasi Telegram terkirim."

    # Foto tidak saling bergantung, jadi dikirim bersamaan agar tidak menunggu
    # timeout satu per satu.
    with ThreadPoolExecutor(max_workers=len(photos)) as pool:
        results = list(pool.map(lambda item: telegram_send_photo(*item), photos))
    if all(result[0] for result in results):
        return True, "Notifikasi Telegram terkirim."
    return False, "Pesan terkirim, tetapi salah satu foto gagal dikirim."


def telegram_notify_request(loan):
    message = (
        "Pengajuan peminjaman baru\n"
        f"Nama: {loan['nama']}\nJenis: {loan['peminjam_type']}\n"
        f"Kelas: {loan['kelas']}\nLab: {loan['lab']}\n"
        f"Tanggal: {loan['tanggal']}\nWaktu: {loan['waktu_mulai']} sampai {loan['waktu_selesai']}\n"
        f"Jumlah murid: {loan['jumlah_murid']}\n"
        "Status: Menunggu persetujuan"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "Setujui", "callback_data": f"approve:{loan['id']}"},
                {"text": "Tolak", "callback_data": f"reject:{loan['id']}"},
            ]
        ]
    }
    photo = get_photo(loan.get("photo_before") or "")
    if photo:
        data, content_type = photo
        ok, _ = telegram_send_photo(
            loan["photo_before"], data, content_type, message, keyboard
        )
        if ok:
            return True, "Notifikasi Telegram terkirim."
    # Jika foto tidak ada atau gagal, teks dan tombol tetap dikirim.
    return telegram_send(message, reply_markup=keyboard)


def telegram_notify_decision(loan, decision):
    message = (
        f"Peminjaman {decision}\n"
        f"Nama: {loan['nama']}\nJenis: {loan['peminjam_type']}\n"
        f"Kelas: {loan['kelas']}\nLab: {loan['lab']}\n"
        f"Tanggal: {loan['tanggal']}\nWaktu: {loan['waktu_mulai']} sampai {loan['waktu_selesai']}"
    )
    return telegram_send(message)


def telegram_api(method, data):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return None
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            data=data,
            timeout=10,
        )
        return response.json()
    except Exception as exc:
        app.logger.warning("Telegram API gagal: %s", type(exc).__name__)
        return None


def telegram_answer_callback(callback_id, text, show_alert=False):
    return telegram_api(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_id,
            "text": text,
            "show_alert": "true" if show_alert else "false",
        },
    )


def telegram_edit_decision(chat_id, message_id, original_text, status):
    lines = [
        line
        for line in str(original_text or "").splitlines()
        if not line.startswith("Status:")
    ]
    text = "\n".join(lines).strip() or "Peminjaman Labkom"
    text += f"\nStatus: {status}"
    return telegram_api(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "reply_markup": json.dumps({"inline_keyboard": []}),
        },
    )


def find_schedule_conflict(c, lab, tanggal, mulai, selesai, exclude_id=None):
    """Return one approved or active overlapping booking for the same lab."""
    query = (
        """SELECT id, nama, lab, tanggal, waktu_mulai, waktu_selesai
           FROM loans
           WHERE lab=? AND tanggal=? AND status IN ('Disetujui','Dipinjam')
             AND waktu_mulai < ? AND waktu_selesai > ?"""
    )
    params = [lab, tanggal, selesai, mulai]
    if exclude_id is not None:
        query += " AND id<>?"
        params.append(exclude_id)
    query += " ORDER BY waktu_mulai LIMIT 1"
    return c.execute(query, tuple(params)).fetchone()


def csv_safe(value):
    """Cegah CSV injection saat file dibuka di Excel."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


VALID_SECTIONS = {"dashboard", "guru", "murid", "riwayat", "admin"}


def redirect_section(section):
    if section not in VALID_SECTIONS:
        section = "dashboard"
    return redirect(url_for("index", _anchor=section))


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
    if "user_name" in session:
        return redirect_section(request.form.get("section", "dashboard"))
    return redirect(url_for("login"))


@app.errorhandler(413)
def handle_too_large(error):
    flash(f"Ukuran unggahan terlalu besar. Maksimal {MAX_UPLOAD_MB} MB.", "error")
    if "user_name" in session:
        return redirect_section("dashboard")
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    try:
        with db() as c:
            c.execute("SELECT 1").fetchone()
        return jsonify({"status": "ok"})
    except Exception:
        app.logger.exception("Health check gagal")
        return jsonify({"status": "error"}), 500


@app.route("/telegram/webhook", methods=["POST"])
@csrf.exempt
def telegram_webhook():
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secret or not hmac.compare_digest(
        supplied.encode("utf-8"), secret.encode("utf-8")
    ):
        abort(403)
    update = request.get_json(silent=True) or {}
    callback = update.get("callback_query")
    if not callback:
        return jsonify({"ok": True})

    callback_id = callback.get("id", "")
    data = callback.get("data", "")
    message = callback.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    message_id = message.get("message_id")
    sender_id = str(callback.get("from", {}).get("id", ""))
    admin_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not admin_chat or admin_chat not in {sender_id, chat_id}:
        telegram_answer_callback(callback_id, "Tidak diizinkan.", show_alert=True)
        return jsonify({"ok": True})

    action, _, loan_id_raw = data.partition(":")
    try:
        loan_id = int(loan_id_raw)
    except ValueError:
        telegram_answer_callback(callback_id, "Data tombol tidak valid.", show_alert=True)
        return jsonify({"ok": True})
    if action not in {"approve", "reject"}:
        telegram_answer_callback(callback_id, "Aksi tidak dikenal.", show_alert=True)
        return jsonify({"ok": True})

    with db(immediate=True) as c:
        loan = c.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        if not loan:
            telegram_answer_callback(callback_id, "Pengajuan tidak ditemukan.", show_alert=True)
            return jsonify({"ok": True})
        if loan["status"] != "Menunggu":
            telegram_answer_callback(callback_id, "Pengajuan sudah diproses.", show_alert=True)
            return jsonify({"ok": True})
        if action == "approve":
            conflict = find_schedule_conflict(
                c,
                loan["lab"],
                loan["tanggal"],
                loan["waktu_mulai"],
                loan["waktu_selesai"],
                exclude_id=loan_id,
            )
            if conflict:
                telegram_answer_callback(
                    callback_id,
                    f"Jadwal bentrok dengan {conflict['nama']} pukul "
                    f"{conflict['waktu_mulai']} sampai {conflict['waktu_selesai']}.",
                    show_alert=True,
                )
                return jsonify({"ok": True})
            status = "Disetujui"
        else:
            status = "Ditolak"
        actor = callback.get("from", {}).get("first_name", "Admin")
        c.execute(
            "UPDATE loans SET status=?, decision_by=?, decision_at=? WHERE id=?",
            (
                status,
                f"{actor} via Telegram",
                datetime.now(WIB).isoformat(timespec="seconds"),
                loan_id,
            ),
        )
        loan = dict(c.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone())

    app.logger.info(
        "AUDIT Telegram %s loan=%s oleh=%s", status, loan_id, sender_id
    )
    telegram_answer_callback(callback_id, f"Peminjaman {status.lower()}.")
    telegram_edit_decision(
        chat_id,
        message_id,
        message.get("text", ""),
        status,
    )
    return jsonify({"ok": True})


@app.before_request
def require_login():
    public = {"login", "static", "healthz", "telegram_webhook"}
    if request.endpoint in public or request.path == "/telegram/webhook":
        return None
    if "user_name" not in session:
        return redirect(url_for("login"))
    with db() as c:
        account = c.execute(
            "SELECT role FROM teachers WHERE lower(nama)=lower(?) AND aktif=1",
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
        "asset_version": ASSET_VERSION,
        # Folder /tmp di Vercel tidak persisten, sehingga fitur bersihkan data
        # hanya ditampilkan jika backup benar-benar bisa disimpan.
        "clear_enabled": not ON_VERCEL,
    }


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=()")
    if request.endpoint == "static" and response.status_code == 200:
        # URL aset selalu memakai ?v=ASSET_VERSION, jadi aman di-cache lama.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store"
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
                "SELECT nama,password_hash,role FROM teachers WHERE lower(nama)=lower(?) AND aktif=1",
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
            existing = c.execute(
                "SELECT id,nama FROM teachers WHERE lower(nama)=lower(?)", (name,)
            ).fetchone()
            password_hash = generate_password_hash(password)
            if existing:
                # Nama lama dipertahankan agar riwayat tetap terhubung.
                c.execute(
                    "UPDATE teachers SET aktif=1, role='teacher', password_hash=? WHERE id=?",
                    (password_hash, existing["id"]),
                )
            else:
                c.execute(
                    "INSERT INTO teachers(nama,aktif,created_at,password_hash,role) "
                    "VALUES(?,1,?,?,'teacher')",
                    (
                        name,
                        datetime.now(WIB).isoformat(timespec="seconds"),
                        password_hash,
                    ),
                )
        app.logger.info(
            "AUDIT simpan guru=%s oleh=%s", name, session.get("user_name")
        )
        flash(f"Akun guru {name} disimpan dan password telah diatur.", "success")
    return redirect_section("admin")


@app.route("/admin/teachers/<int:teacher_id>/delete", methods=["POST"])
def delete_teacher(teacher_id):
    if session.get("role") != "admin":
        abort(403)
    with db() as c:
        teacher = c.execute(
            "SELECT nama,role FROM teachers WHERE id=?", (teacher_id,)
        ).fetchone()
        if not teacher:
            flash("Akun guru tidak ditemukan.", "error")
            return redirect_section("admin")
        if teacher["role"] == "admin":
            flash("Akun admin tidak bisa dinonaktifkan.", "error")
            return redirect_section("admin")
        c.execute("UPDATE teachers SET aktif=0 WHERE id=?", (teacher_id,))
        teacher_name = teacher["nama"]
    app.logger.info(
        "AUDIT nonaktifkan guru=%s oleh=%s", teacher_name, session.get("user_name")
    )
    flash("Akun guru dinonaktifkan. Riwayat peminjaman tidak dihapus.", "success")
    return redirect_section("admin")


@app.route("/admin/teachers/<int:teacher_id>/activate", methods=["POST"])
def activate_teacher(teacher_id):
    if session.get("role") != "admin":
        abort(403)
    with db() as c:
        teacher = c.execute(
            "SELECT nama,role FROM teachers WHERE id=?", (teacher_id,)
        ).fetchone()
        if not teacher:
            flash("Akun guru tidak ditemukan.", "error")
            return redirect_section("admin")
        if teacher["role"] == "admin":
            flash("Akun admin tidak perlu diaktifkan.", "error")
            return redirect_section("admin")
        c.execute("UPDATE teachers SET aktif=1 WHERE id=?", (teacher_id,))
        teacher_name = teacher["nama"]
    app.logger.info(
        "AUDIT aktifkan guru=%s oleh=%s", teacher_name, session.get("user_name")
    )
    flash("Akun guru diaktifkan kembali.", "success")
    return redirect_section("admin")


@app.route("/admin/loans/<int:loan_id>/approve", methods=["POST"])
def approve_loan(loan_id):
    if session.get("role") != "admin":
        abort(403)
    with db(immediate=True) as c:
        loan = c.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        if not loan:
            flash("Pengajuan tidak ditemukan.", "error")
            return redirect_section("admin")
        if loan["status"] != "Menunggu":
            flash("Pengajuan ini sudah diproses.", "error")
            return redirect_section("admin")
        conflict = find_schedule_conflict(
            c,
            loan["lab"],
            loan["tanggal"],
            loan["waktu_mulai"],
            loan["waktu_selesai"],
            exclude_id=loan_id,
        )
        if conflict:
            flash(
                f"Tidak bisa disetujui. {loan['lab']} sudah dibooking "
                f"{conflict['nama']} pukul {conflict['waktu_mulai']} sampai "
                f"{conflict['waktu_selesai']}.",
                "error",
            )
            return redirect_section("admin")
        c.execute(
            "UPDATE loans SET status='Disetujui', decision_by=?, decision_at=? WHERE id=?",
            (session.get("user_name", ""), datetime.now(WIB).isoformat(timespec="seconds"), loan_id),
        )
        loan = dict(c.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone())
    app.logger.info(
        "AUDIT setujui loan=%s oleh=%s", loan_id, session.get("user_name", "-")
    )
    if telegram_configured():
        telegram_notify_decision(loan, "disetujui")
    flash("Pengajuan peminjaman disetujui.", "success")
    return redirect_section("admin")


@app.route("/admin/loans/<int:loan_id>/reject", methods=["POST"])
def reject_loan(loan_id):
    if session.get("role") != "admin":
        abort(403)
    with db() as c:
        loan = c.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        if not loan:
            flash("Pengajuan tidak ditemukan.", "error")
            return redirect_section("admin")
        if loan["status"] != "Menunggu":
            flash("Pengajuan ini sudah diproses.", "error")
            return redirect_section("admin")
        c.execute(
            "UPDATE loans SET status='Ditolak', decision_by=?, decision_at=? WHERE id=?",
            (session.get("user_name", ""), datetime.now(WIB).isoformat(timespec="seconds"), loan_id),
        )
        loan = dict(c.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone())
    app.logger.info(
        "AUDIT tolak loan=%s oleh=%s", loan_id, session.get("user_name", "-")
    )
    if telegram_configured():
        telegram_notify_decision(loan, "ditolak")
    flash("Pengajuan peminjaman ditolak.", "success")
    return redirect_section("admin")


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
    return redirect_section("admin")


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
            return redirect_section("admin")
        c.execute("DELETE FROM students WHERE nis=?", (nis,))
    app.logger.info("AUDIT hapus murid=%s oleh=%s", nis, session.get("user_name"))
    flash("Data murid dihapus.", "success")
    return redirect_section("admin")


@app.route("/")
def index():
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    now = datetime.now(WIB)
    today = now.strftime("%Y-%m-%d")
    now_time = now.strftime("%H:%M")

    with db() as c:
        if session.get("role") == "admin":
            stats = c.execute(
                """SELECT
                   (SELECT COUNT(*) FROM loans) AS total_loans,
                   (SELECT COUNT(*) FROM students) AS students_count,
                   (SELECT COUNT(*) FROM loans
                    WHERE status IN ('Disetujui','Dipinjam')) AS active"""
            ).fetchone()
        else:
            stats = c.execute(
                """SELECT
                   (SELECT COUNT(*) FROM loans WHERE created_by=?) AS total_loans,
                   (SELECT COUNT(*) FROM students) AS students_count,
                   (SELECT COUNT(*) FROM loans
                    WHERE status IN ('Disetujui','Dipinjam')) AS active""",
                (session["user_name"],),
            ).fetchone()
        total_loans = stats["total_loans"]
        students_count = stats["students_count"]
        active = stats["active"]
        pages = max(1, (total_loans + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        offset = (page - 1) * PAGE_SIZE

        if session.get("role") == "admin":
            loans = c.execute(
                "SELECT * FROM loans ORDER BY id DESC LIMIT ? OFFSET ?",
                (PAGE_SIZE, offset),
            ).fetchall()
            active_loans = c.execute(
                "SELECT * FROM loans WHERE status IN ('Disetujui','Dipinjam') "
                "ORDER BY created_at ASC"
            ).fetchall()
            pending_loans = c.execute(
                "SELECT * FROM loans WHERE status='Menunggu' ORDER BY created_at ASC"
            ).fetchall()
            schedule = c.execute(
                "SELECT lab,tanggal,waktu_mulai,waktu_selesai FROM loans "
                "WHERE status IN ('Disetujui','Dipinjam') AND tanggal>=? "
                "ORDER BY tanggal,waktu_mulai LIMIT 100",
                (today,),
            ).fetchall()
            teacher_rows = c.execute(
                "SELECT id,nama,aktif FROM teachers "
                "WHERE role='teacher' ORDER BY nama"
            ).fetchall()
            teachers = [row for row in teacher_rows if row["aktif"]]
            inactive_teachers = [row for row in teacher_rows if not row["aktif"]]
            students = c.execute(
                "SELECT nis,nama,kelas FROM students ORDER BY kelas, nama"
            ).fetchall()
        else:
            loans = c.execute(
                "SELECT * FROM loans WHERE created_by=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (session["user_name"], PAGE_SIZE, offset),
            ).fetchall()
            active_loans = c.execute(
                "SELECT * FROM loans WHERE status IN ('Disetujui','Dipinjam') "
                "AND created_by=? ORDER BY created_at ASC",
                (session["user_name"],),
            ).fetchall()
            pending_loans = c.execute(
                "SELECT * FROM loans WHERE status='Menunggu' AND created_by=? "
                "ORDER BY created_at ASC",
                (session["user_name"],),
            ).fetchall()
            schedule = c.execute(
                "SELECT lab,tanggal,waktu_mulai,waktu_selesai FROM loans "
                "WHERE status IN ('Disetujui','Dipinjam') AND tanggal>=? "
                "ORDER BY tanggal,waktu_mulai LIMIT 100",
                (today,),
            ).fetchall()
            teachers = []
            inactive_teachers = []
            students = []

    return render_template(
        "index.html",
        loans=loans,
        students_count=students_count,
        active=active,
        schedule=schedule,
        active_loans=active_loans,
        pending_loans=pending_loans,
        teachers=teachers,
        inactive_teachers=inactive_teachers,
        students=students,
        today=today,
        now_time=now_time,
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


@app.route("/api/bookings")
def booking_lookup():
    lab = request.args.get("lab", "").strip()
    if lab not in ["Labkom 1", "Labkom 2"]:
        return jsonify({"error": "Lab tidak valid"}), 400
    today = datetime.now(WIB).strftime("%Y-%m-%d")
    with db() as c:
        rows = c.execute(
            """SELECT tanggal,waktu_mulai,waktu_selesai,nama,peminjam_type,kelas
               FROM loans
               WHERE lab=? AND status IN ('Disetujui','Dipinjam') AND tanggal>=?
               ORDER BY tanggal,waktu_mulai LIMIT 50""",
            (lab, today),
        ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.route("/borrow/student", methods=["POST"])
def borrow_student():
    nis = request.form.get("nis", " ").strip()
    lab = request.form.get("lab", "").strip()
    duration_raw = request.form.get("durasi", "60").strip()
    with db(immediate=True) as c:
        s = c.execute("SELECT * FROM students WHERE nis=?", (nis,)).fetchone()
        if not s:
            flash("NIS tidak ditemukan dalam data murid.", "error")
            return redirect_section("murid")
        if lab not in ["Labkom 1", "Labkom 2"]:
            flash("Pilih Labkom 1 atau Labkom 2.", "error")
            return redirect_section("murid")
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
            return redirect_section("murid")
        conflict = find_schedule_conflict(c, lab, tanggal, mulai, selesai)
        if conflict:
            flash(
                f"Jadwal bentrok: {lab} sudah dibooking pukul "
                f"{conflict['waktu_mulai']} sampai {conflict['waktu_selesai']} pada tanggal {tanggal}. "
                "Pilih lab atau waktu lain.",
                "error",
            )
            return redirect_section("murid")
        try:
            created = c.execute(
                """INSERT INTO loans(peminjam_type,nama,kelas,nis,lab,tanggal,waktu_mulai,waktu_selesai,status,created_at,created_by)
                  VALUES('Murid',?,?,?,?,?,?,?,'Dipinjam',?,?) RETURNING id""",
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
            ).fetchone()
            for class_name in parse_classes(s["kelas"]):
                c.execute(
                    "INSERT INTO loan_classes(loan_id,kelas) VALUES(?,?) "
                    "ON CONFLICT (loan_id,kelas) DO NOTHING",
                    (created["id"], class_name),
                )
        except INTEGRITY_ERRORS:
            flash(
                "Murid ini masih memiliki peminjaman aktif. "
                "Selesaikan transaksi tersebut terlebih dahulu.",
                "error",
            )
            return redirect_section("murid")
    flash(
        f"Peminjaman murid {s['nama']} tercatat. Mulai {mulai}, estimasi selesai {selesai}.",
        "success",
    )
    return redirect_section("murid")


@app.route("/borrow/teacher", methods=["POST"])
def borrow_teacher():
    nama = session.get("user_name", "")
    kelas_raw = request.form.get("kelas", "").strip()
    classes = parse_classes(kelas_raw)
    jumlah = request.form.get("jumlah_murid", "0").strip()
    lab = request.form.get("lab", "").strip()
    tanggal = request.form.get("tanggal", "").strip()
    mulai = request.form.get("waktu_mulai", "").strip()
    selesai = request.form.get("waktu_selesai", "").strip()
    photo_before_file = request.files.get("photo_before")

    if not all([nama, classes, tanggal, mulai, selesai]) or lab not in ["Labkom 1", "Labkom 2"]:
        flash("Periksa kembali seluruh isian guru dan rentang waktunya.", "error")
        return redirect_section("guru")
    if len(classes) > 20:
        flash("Maksimal 20 kelas dalam satu peminjaman.", "error")
        return redirect_section("guru")
    try:
        booking_date = datetime.strptime(tanggal, "%Y-%m-%d").date()
        start_time = datetime.strptime(mulai, "%H:%M").time()
        end_time = datetime.strptime(selesai, "%H:%M").time()
    except ValueError:
        flash("Format tanggal atau jam tidak valid.", "error")
        return redirect_section("guru")
    now = datetime.now(WIB)
    current_minute = now.time().replace(second=0, microsecond=0)
    if booking_date < now.date() or (
        booking_date == now.date() and start_time < current_minute
    ):
        flash("Tanggal dan jam peminjaman tidak boleh di masa lalu.", "error")
        return redirect_section("guru")
    if end_time <= start_time:
        flash("Jam selesai harus lebih akhir dari jam mulai.", "error")
        return redirect_section("guru")
    if not photo_before_file or not photo_before_file.filename:
        flash(
            "Guru wajib mengunggah foto kondisi awal Labkom dari HP sebelum menyimpan peminjaman.",
            "error",
        )
        return redirect_section("guru")
    try:
        jumlah = int(jumlah)
    except (TypeError, ValueError):
        jumlah = 0
    if jumlah < 1 or jumlah > 1000:
        flash("Jumlah murid harus antara 1 sampai 1000.", "error")
        return redirect_section("guru")
    kelas = ", ".join(classes)
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
            return redirect_section("guru")
        try:
            photo_before = save_uploaded_photo(photo_before_file, "awal_guru")
        except ValueError as e:
            flash(str(e), "error")
            return redirect_section("guru")
        try:
            created = c.execute(
                """INSERT INTO loans(peminjam_type,nama,kelas,jumlah_murid,lab,tanggal,waktu_mulai,waktu_selesai,status,created_at,photo_before,created_by)
                  VALUES('Guru',?,?,?,?,?,?,?,'Menunggu',?,?,?) RETURNING id""",
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
            ).fetchone()
            for class_name in classes:
                c.execute(
                    "INSERT INTO loan_classes(loan_id,kelas) VALUES(?,?) "
                    "ON CONFLICT (loan_id,kelas) DO NOTHING",
                    (created["id"], class_name),
                )
        except Exception:
            remove_uploaded_photo(photo_before)
            app.logger.exception("Gagal menyimpan peminjaman guru")
            raise
    app.logger.info(
        "AUDIT pengajuan guru=%s lab=%s tanggal=%s %s-%s", nama, lab, tanggal, mulai, selesai
    )
    with db() as c:
        loan_row = c.execute(
            "SELECT * FROM loans WHERE id=?", (created["id"],)
        ).fetchone()
    loan = dict(loan_row)
    if telegram_configured():
        ok, _ = telegram_notify_request(loan)
        if not ok:
            flash(
                "Pengajuan dikirim dan menunggu persetujuan admin. "
                "Notifikasi Telegram gagal dikirim.",
                "warning",
            )
            return redirect_section("guru")
    flash(
        "Pengajuan dikirim dan menunggu persetujuan admin.",
        "success",
    )
    return redirect_section("guru")


@app.route("/return/<int:loan_id>", methods=["GET", "POST"])
def return_loan(loan_id):
    with db() as c:
        row = c.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not row:
        flash("Data peminjaman tidak ditemukan.", "error")
        return redirect_section("riwayat")
    loan = dict(row)
    if session.get("role") != "admin" and loan.get("created_by") != session.get("user_name"):
        abort(403)
    if loan["status"] == "Selesai":
        flash("Peminjaman ini sudah ditandai selesai.", "error")
        return redirect_section("riwayat")
    if loan["status"] == "Menunggu":
        flash("Peminjaman ini belum disetujui admin.", "error")
        return redirect_section("riwayat")
    if loan["status"] == "Ditolak":
        flash("Peminjaman ini ditolak admin.", "error")
        return redirect_section("riwayat")
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
    if telegram_configured():
        ok, _ = telegram_notify(loan)
        if ok:
            flash("Peminjaman ditandai selesai. Notifikasi Telegram terkirim.", "success")
        else:
            flash("Peminjaman ditandai selesai. Notifikasi Telegram gagal.", "warning")
    else:
        flash(
            "Peminjaman ditandai selesai. Telegram belum dikonfigurasi.",
            "warning",
        )
    return redirect_section("riwayat")


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
            for table in ("loans", "loan_classes", "teachers", "students")
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
    if ON_VERCEL:
        flash(
            "Pembersihan dinonaktifkan di Vercel karena folder backup bersifat sementara. "
            "Gunakan snapshot Neon untuk pemulihan data.",
            "error",
        )
        return redirect_section("admin")
    if request.form.get("confirmation", "").strip() != "BERSIHKAN":
        flash("Penghapusan dibatalkan. Konfirmasi tidak sesuai.", "error")
        return redirect_section("admin")

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
        return redirect_section("admin")
    finally:
        if temp_db and os.path.exists(temp_db):
            try:
                os.remove(temp_db)
            except OSError:
                pass

    try:
        with db() as c:
            c.execute("DELETE FROM loan_classes")
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
        return redirect_section("admin")

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
    return redirect_section("admin")


@app.route("/loan/<int:loan_id>/photos")
def loan_photos(loan_id):
    with db() as c:
        row = c.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not row:
        abort(404)
    loan = dict(row)
    if session.get("role") != "admin" and loan.get("created_by") != session.get("user_name"):
        abort(403)
    photos = {}
    for field in ("photo_before", "photo_after"):
        filename = loan.get(field) or ""
        if not filename:
            continue
        photo = get_photo(filename)
        if photo:
            data, content_type = photo
            photos[field] = {
                "filename": filename,
                "content_type": content_type,
                "size_kb": max(1, len(data) // 1024),
            }
    return render_template("photos.html", loan=loan, photos=photos)


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
    # Nama file foto selalu unik dan tidak pernah ditimpa.
    response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
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
