import os

# Harus di-set sebelum app.py diimpor oleh test.
os.environ["FLASK_SECRET_KEY"] = "test-secret-key"
os.environ["ADMIN_NAME"] = "Rahma Maulidina"
os.environ["ADMIN_PASSWORD"] = "test-admin-password"
os.environ["LABKOM_SKIP_INIT"] = "1"
os.environ["MAX_UPLOAD_MB"] = "1"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["TELEGRAM_WEBHOOK_SECRET"] = "test-webhook-secret"
