"""StockLedger: stock, quotes, invoices and simple UK sole-trader accounting."""
import os
import secrets
from datetime import timedelta

from flask import Flask

DATA_DIR = os.environ.get("DATA_DIR", "/data")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/backups")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "stockledger.db")

for _d in (DATA_DIR, BACKUP_DIR, UPLOAD_DIR, os.path.join(UPLOAD_DIR, "receipts")):
    os.makedirs(_d, exist_ok=True)

# The signing key for login sessions is generated once and kept in the data folder.
_key_file = os.path.join(DATA_DIR, "secret.key")
if not os.path.exists(_key_file):
    with open(_key_file, "w") as fh:
        fh.write(secrets.token_hex(32))
    os.chmod(_key_file, 0o600)
with open(_key_file) as fh:
    _secret = fh.read().strip()

app = Flask(__name__)
app.config.update(
    SECRET_KEY=_secret,
    DATA_DIR=DATA_DIR,
    BACKUP_DIR=BACKUP_DIR,
    UPLOAD_DIR=UPLOAD_DIR,
    DB_PATH=DB_PATH,
    BACKUP_HOUR=int(os.environ.get("BACKUP_HOUR", "2")),
    BACKUP_KEEP=int(os.environ.get("BACKUP_KEEP", "30")),
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("COOKIE_SECURE")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    TEMPLATES_AUTO_RELOAD=False,
)

from . import db  # noqa: E402

db.init_db(DB_PATH)
app.teardown_appcontext(db.close_db)

from . import views  # noqa: E402,F401

if not os.environ.get("NO_BACKUP_THREAD"):
    from .backup import start_scheduler

    start_scheduler(app)
