#!/usr/bin/env python3
"""Start-up helper: fix folder ownership, drop root, then launch gunicorn.

Unraid convention is PUID=99 (nobody) and PGID=100 (users) so files in appdata
stay readable from your other shares.
"""
import os
import sys

puid = int(os.environ.get("PUID", "99"))
pgid = int(os.environ.get("PGID", "100"))
port = os.environ.get("PORT", "8080")

if os.geteuid() == 0:
    for path in (os.environ.get("DATA_DIR", "/data"), os.environ.get("BACKUP_DIR", "/backups")):
        os.makedirs(path, exist_ok=True)
        for root, dirs, files in os.walk(path):
            for name in [root] + [os.path.join(root, n) for n in dirs + files]:
                try:
                    os.chown(name, puid, pgid)
                except OSError:
                    pass
    os.setgroups([])
    os.setgid(pgid)
    os.setuid(puid)
    os.environ["HOME"] = "/tmp"

os.execvp("gunicorn", [
    "gunicorn", "--bind", f"0.0.0.0:{port}",
    "--workers", "1", "--threads", "4",   # one worker: the nightly backup thread must only run once
    "--access-logfile", "-", "wsgi:app",
])
sys.exit(1)
