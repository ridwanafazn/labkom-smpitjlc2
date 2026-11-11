import io
import json
import os
import sqlite3
import sys
import zipfile

import pyzipper
import pytest
from PIL import Image
from werkzeug.datastructures import FileStorage
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as labkom


@pytest.fixture
def client(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    backup_dir = tmp_path / "backup"
    monkeypatch.setattr(labkom, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(labkom, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(labkom, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(labkom, "STUDENTS_PATH", str(tmp_path / "students.json"))
    monkeypatch.setattr(labkom, "BACKUP_KEEP", 10)
    monkeypatch.setattr(labkom, "BACKUP_COPY_DIR", "")
    monkeypatch.setattr(labkom, "BACKUP_PASSWORD", "")
    upload_dir.mkdir()
    labkom.LOGIN_ATTEMPTS.clear()
    labkom.init_db()
    labkom.app.config.update(
        TESTING=True,
        SECRET_KEY="test-secret",
        WTF_CSRF_ENABLED=False,
    )
    with labkom.app.test_client() as test_client:
        yield test_client


def login(client, name="Rahma Maulidina", role="admin"):
    with client.session_transaction() as sess:
        sess["user_name"] = name
        sess["role"] = role


def add_teacher(name="Guru A", aktif=1, password="password123"):
    with labkom.db() as c:
        c.execute(
            "INSERT OR REPLACE INTO teachers(nama,aktif,created_at,password_hash,role) "
            "VALUES(?,?,?,?,'teacher')",
            (name, aktif, "2026-11-06T00:00:00", generate_password_hash(password)),
        )


def add_student(nis="123", nama="Murid A", kelas="7A"):
    with labkom.db() as c:
        c.execute(
            "INSERT OR REPLACE INTO students(nis,nama,kelas) VALUES(?,?,?)",
            (nis, nama, kelas),
        )


def insert_loan(**overrides):
    data = {
        "peminjam_type": "Guru",
        "nama": "Guru A",
        "kelas": "9A",
        "jumlah_murid": 10,
        "nis": "",
        "lab": "Labkom 1",
        "tanggal": "2026-11-06",
        "waktu_mulai": "10:00",
        "waktu_selesai": "11:00",
        "status": "Dipinjam",
        "created_at": "2026-11-06T10:00:00",
        "returned_at": "",
        "photo_before": "",
        "photo_after": "",
        "created_by": "Guru A",
        "completed_by": "",
    }
    data.update(overrides)
    with labkom.db() as c:
        cur = c.execute(
            """INSERT INTO loans(
                peminjam_type,nama,kelas,jumlah_murid,nis,lab,tanggal,
                waktu_mulai,waktu_selesai,status,created_at,returned_at,
                photo_before,photo_after,created_by,completed_by
            ) VALUES(
                :peminjam_type,:nama,:kelas,:jumlah_murid,:nis,:lab,:tanggal,
                :waktu_mulai,:waktu_selesai,:status,:created_at,:returned_at,
                :photo_before,:photo_after,:created_by,:completed_by
            )""",
            data,
        )
        return cur.lastrowid


def test_login_requires_correct_password(client):
    response = client.post(
        "/login",
        data={"nama": "Rahma Maulidina", "password": "salah"},
        follow_redirects=True,
    )
    assert b"Nama atau password salah" in response.data
    with client.session_transaction() as sess:
        assert "user_name" not in sess

    client.post(
        "/login",
        data={"nama": "Rahma Maulidina", "password": "test-admin-password"},
    )
    with client.session_transaction() as sess:
        assert sess["user_name"] == "Rahma Maulidina"
        assert sess["role"] == "admin"


def test_teacher_login_uses_password(client):
    add_teacher("Guru A", password="password123")
    client.post("/login", data={"nama": "Guru A", "password": "password123"})
    with client.session_transaction() as sess:
        assert sess["user_name"] == "Guru A"
        assert sess["role"] == "teacher"


def test_login_rate_limited(client):
    for _ in range(labkom.LOGIN_MAX_ATTEMPTS):
        client.post(
            "/login",
            data={"nama": "Rahma Maulidina", "password": "salah"},
        )
    response = client.post(
        "/login",
        data={"nama": "Rahma Maulidina", "password": "test-admin-password"},
        follow_redirects=True,
    )
    assert b"Terlalu banyak percobaan login" in response.data
    with client.session_transaction() as sess:
        assert "user_name" not in sess


def test_csrf_rejects_post_without_token(client):
    labkom.app.config["WTF_CSRF_ENABLED"] = True
    try:
        response = client.post(
            "/login",
            data={"nama": "Rahma Maulidina", "password": "test-admin-password"},
        )
        assert response.status_code == 302
        with client.session_transaction() as sess:
            assert "user_name" not in sess
    finally:
        labkom.app.config["WTF_CSRF_ENABLED"] = False


def jpeg_bytes(color="red"):
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), color).save(buffer, "JPEG")
    return buffer.getvalue()


def test_conflict_detection(client):
    insert_loan(tanggal="2026-11-06", waktu_mulai="10:00", waktu_selesai="11:00")
    with labkom.db() as c:
        assert labkom.find_schedule_conflict(c, "Labkom 1", "2026-11-06", "10:30", "10:45")
        assert not labkom.find_schedule_conflict(c, "Labkom 2", "2026-11-06", "10:30", "10:45")
        assert not labkom.find_schedule_conflict(c, "Labkom 1", "2026-11-06", "11:00", "12:00")


def test_duplicate_active_student_rejected(client):
    add_teacher("Guru A")
    add_student()
    login(client, "Guru A", "teacher")
    client.post("/borrow/student", data={"nis": "123", "lab": "Labkom 1", "durasi": "60"})
    client.post(
        "/borrow/student",
        data={"nis": "123", "lab": "Labkom 2", "durasi": "60"},
        follow_redirects=True,
    )
    with labkom.db() as c:
        count = c.execute(
            "SELECT COUNT(*) n FROM loans WHERE nis='123' AND status='Dipinjam'"
        ).fetchone()["n"]
    assert count == 1


def test_teacher_cannot_open_other_loan(client):
    add_teacher("Guru A")
    add_teacher("Guru B")
    loan_id = insert_loan(created_by="Guru A")
    login(client, "Guru B", "teacher")
    assert client.get(f"/return/{loan_id}").status_code == 403


def test_upload_validation_and_orphan_cleanup(client):
    with pytest.raises(ValueError):
        labkom.save_uploaded_photo(
            FileStorage(stream=io.BytesIO(b"x"), filename="foto.txt"), "awal"
        )

    filename = labkom.save_uploaded_photo(
        FileStorage(stream=io.BytesIO(jpeg_bytes()), filename="foto.jpg"), "awal"
    )
    assert os.path.isfile(os.path.join(labkom.UPLOAD_DIR, filename))
    labkom.remove_uploaded_photo(filename)
    assert not os.path.exists(os.path.join(labkom.UPLOAD_DIR, filename))

    # Foto tidak boleh tersimpan jika jadwal ternyata bentrok.
    add_teacher("Guru A")
    insert_loan(tanggal="2026-11-06", waktu_mulai="10:00", waktu_selesai="11:00")
    login(client, "Guru A", "teacher")
    client.post(
        "/borrow/teacher",
        data={
            "kelas": "9A",
            "jumlah_murid": "10",
            "lab": "Labkom 1",
            "tanggal": "2026-11-06",
            "waktu_mulai": "10:30",
            "waktu_selesai": "10:45",
            "photo_before": (io.BytesIO(jpeg_bytes()), "awal.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert os.listdir(labkom.UPLOAD_DIR) == []


def test_upload_size_limit(client):
    add_teacher("Guru A")
    login(client, "Guru A", "teacher")
    too_big = b"x" * (labkom.MAX_UPLOAD_MB * 1024 * 1024 + 1024)
    response = client.post(
        "/borrow/teacher",
        data={
            "kelas": "9A",
            "jumlah_murid": "10",
            "lab": "Labkom 1",
            "tanggal": "2026-11-06",
            "waktu_mulai": "10:30",
            "waktu_selesai": "10:45",
            "photo_before": (io.BytesIO(too_big), "besar.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert os.listdir(labkom.UPLOAD_DIR) == []


def test_backup_before_clear(client):
    add_teacher("Guru A")
    insert_loan(photo_before="foto.jpg")
    with open(os.path.join(labkom.UPLOAD_DIR, "foto.jpg"), "wb") as f:
        f.write(b"foto")
    login(client)
    response = client.post(
        "/admin/clear-loans", data={"confirmation": "BERSIHKAN"}, follow_redirects=True
    )
    assert response.status_code == 200

    with labkom.db() as c:
        assert c.execute("SELECT COUNT(*) n FROM loans").fetchone()["n"] == 0
        assert (
            c.execute("SELECT COUNT(*) n FROM teachers WHERE role='teacher'").fetchone()["n"]
            == 1
        )

    backups = [
        os.path.join(labkom.BACKUP_DIR, name)
        for name in os.listdir(labkom.BACKUP_DIR)
        if name.endswith(".zip")
    ]
    assert len(backups) == 1
    with zipfile.ZipFile(backups[0]) as archive:
        names = archive.namelist()
        assert "labkom.db" in names
        assert "uploads_labkom/foto.jpg" in names
    assert os.listdir(labkom.UPLOAD_DIR) == []


def test_backup_retention(client, monkeypatch):
    monkeypatch.setattr(labkom, "BACKUP_KEEP", 1)
    os.makedirs(labkom.BACKUP_DIR, exist_ok=True)
    old_backup = os.path.join(labkom.BACKUP_DIR, "cadangan_sebelum_bersih_20000101_000000.zip")
    with zipfile.ZipFile(old_backup, "w") as archive:
        archive.writestr("lama.txt", "lama")
    login(client)
    client.post("/admin/clear-loans", data={"confirmation": "BERSIHKAN"})
    assert not os.path.exists(old_backup)
    assert len([n for n in os.listdir(labkom.BACKUP_DIR) if n.endswith(".zip")]) == 1


def test_backup_encryption_and_offsite_copy(client, monkeypatch, tmp_path):
    offsite = tmp_path / "offsite"
    monkeypatch.setattr(labkom, "BACKUP_PASSWORD", "rahasia")
    monkeypatch.setattr(labkom, "BACKUP_COPY_DIR", str(offsite))
    login(client)
    client.post("/admin/clear-loans", data={"confirmation": "BERSIHKAN"})

    backups = [n for n in os.listdir(labkom.BACKUP_DIR) if n.endswith(".zip")]
    assert len(backups) == 1
    with pyzipper.AESZipFile(os.path.join(labkom.BACKUP_DIR, backups[0])) as archive:
        archive.setpassword(b"rahasia")
        assert "labkom.db" in archive.namelist()
    assert os.path.isfile(os.path.join(offsite, backups[0]))


def test_inactive_teacher_session_rejected(client):
    add_teacher("Guru A", aktif=0)
    login(client, "Guru A", "teacher")
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    with client.session_transaction() as sess:
        assert "user_name" not in sess


def test_readd_teacher_reactivates_and_hashes_password(client):
    add_teacher("Guru A", aktif=0)
    login(client)
    client.post(
        "/admin/teachers",
        data={"nama": "Guru A", "password": "password-baru"},
    )
    with labkom.db() as c:
        row = c.execute(
            "SELECT aktif,password_hash FROM teachers WHERE nama='Guru A'"
        ).fetchone()
    assert row["aktif"] == 1
    assert row["password_hash"] != "password-baru"
    assert "password-baru" not in row["password_hash"]


def test_partial_unique_index_blocks_duplicate_active_student(client):
    add_student()
    insert_loan(peminjam_type="Murid", nis="123", created_by="Guru A")
    with pytest.raises(sqlite3.IntegrityError):
        insert_loan(peminjam_type="Murid", nis="123", created_by="Guru A")


def test_csv_escapes_formula_and_has_bom(client):
    insert_loan(nama="=cmd")
    login(client)
    response = client.get("/export.csv")
    assert response.status_code == 200
    assert response.data.startswith(b"\xef\xbb\xbf")
    assert b"'=cmd" in response.data


def test_photo_access_ownership(client):
    add_teacher("Guru A")
    add_teacher("Guru B")
    with open(os.path.join(labkom.UPLOAD_DIR, "a.jpg"), "wb") as f:
        f.write(b"a")
    with open(os.path.join(labkom.UPLOAD_DIR, "b.jpg"), "wb") as f:
        f.write(b"b")
    insert_loan(created_by="Guru A", photo_before="a.jpg")
    insert_loan(created_by="Guru B", photo_before="b.jpg")

    login(client, "Guru A", "teacher")
    assert client.get("/uploads_labkom/a.jpg").status_code == 200
    assert client.get("/uploads_labkom/b.jpg").status_code == 403

    login(client)
    response = client.get("/uploads_labkom/b.jpg")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_admin_manages_students(client):
    login(client)
    response = client.post(
        "/admin/students",
        data={"nis": "777", "nama": "Murid C", "kelas": "7B"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    with labkom.db() as c:
        row = c.execute("SELECT nama,kelas FROM students WHERE nis='777'").fetchone()
    assert row["nama"] == "Murid C"
    assert row["kelas"] == "7B"

    client.post("/admin/students/777/delete", follow_redirects=True)
    with labkom.db() as c:
        assert c.execute("SELECT COUNT(*) n FROM students WHERE nis='777'").fetchone()["n"] == 0


def test_teacher_cannot_manage_students(client):
    add_teacher("Guru A")
    login(client, "Guru A", "teacher")
    assert (
        client.post(
            "/admin/students",
            data={"nis": "778", "nama": "Murid D", "kelas": "7C"},
        ).status_code
        == 403
    )


def test_custom_404_page(client):
    login(client)
    response = client.get("/halaman-tidak-ada")
    assert response.status_code == 404
    assert b"Halaman tidak ditemukan" in response.data


def test_pagination(client):
    for i in range(55):
        insert_loan(nama=f"Guru {i}")
    login(client)
    first = client.get("/")
    second = client.get("/?page=2")
    assert b"dari 55 transaksi" in first.data
    assert b"Menampilkan 1" in first.data
    assert b"Menampilkan 51" in second.data


def test_students_json_import_is_robust(client, monkeypatch):
    students_path = os.path.join(os.path.dirname(labkom.DB_PATH), "students.json")
    monkeypatch.setattr(labkom, "STUDENTS_PATH", students_path)
    with open(students_path, "w", encoding="utf-8") as f:
        f.write("{bukan json")
    labkom.init_db()

    with open(students_path, "w", encoding="utf-8") as f:
        json.dump([{"nis": "999", "nama": "Murid B", "kelas": "8A"}], f)
    labkom.init_db()

    with labkom.db() as c:
        row = c.execute("SELECT nama FROM students WHERE nis='999'").fetchone()
    assert row["nama"] == "Murid B"
