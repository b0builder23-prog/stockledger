import datetime as dt
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import zipfile

log = logging.getLogger("stockledger.backup")
NAME_RE = re.compile(r"^stockledger-\d{8}-\d{6}\.zip$")


def make_backup(db_path, upload_dir, backup_dir, keep=30):
    """Write a consistent copy of the database plus uploads into a dated zip."""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    final = os.path.join(backup_dir, f"stockledger-{stamp}.zip")
    part = final + ".part"
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "stockledger.db")
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(copy)
        with dst:
            src.backup(dst)  # safe even while the app is writing
        src.close()
        dst.close()
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(copy, "stockledger.db")
            for root, _dirs, files in os.walk(upload_dir):
                for name in files:
                    full = os.path.join(root, name)
                    z.write(full, os.path.join("uploads", os.path.relpath(full, upload_dir)))
    os.replace(part, final)
    prune(backup_dir, keep)
    return os.path.basename(final)


def list_backups(backup_dir):
    if not os.path.isdir(backup_dir):
        return []
    out = []
    for name in sorted(os.listdir(backup_dir), reverse=True):
        if NAME_RE.match(name):
            st = os.stat(os.path.join(backup_dir, name))
            out.append({"name": name, "size": st.st_size, "when": dt.datetime.fromtimestamp(st.st_mtime)})
    return out


def prune(backup_dir, keep):
    names = [b["name"] for b in list_backups(backup_dir)]
    for name in names[keep:]:
        try:
            os.remove(os.path.join(backup_dir, name))
        except OSError:
            log.exception("could not remove old backup %s", name)


def _backed_up_today(backup_dir):
    today = dt.date.today().strftime("%Y%m%d")
    return any(b["name"].startswith(f"stockledger-{today}") for b in list_backups(backup_dir))


def start_scheduler(app):
    cfg = app.config

    def loop():
        while True:
            try:
                now = dt.datetime.now()
                if now.hour >= cfg["BACKUP_HOUR"] and not _backed_up_today(cfg["BACKUP_DIR"]):
                    name = make_backup(cfg["DB_PATH"], cfg["UPLOAD_DIR"], cfg["BACKUP_DIR"], cfg["BACKUP_KEEP"])
                    log.warning("nightly backup written: %s", name)
            except Exception:  # never let the thread die
                log.exception("nightly backup failed")
            time.sleep(300)

    threading.Thread(target=loop, name="nightly-backup", daemon=True).start()
