"""Run every test script; fail if any exits non-zero or prints FAIL."""
import os, subprocess, sys

here = os.path.dirname(os.path.abspath(__file__))
bad = False
for name in ("e2e.py", "issue_fix.py", "goods.py"):
    r = subprocess.run([sys.executable, os.path.join(here, name)], capture_output=True, text=True)
    failed = r.returncode != 0 or "FAIL" in r.stdout.replace("FAILED: none", "")
    print(("FAILED  " if failed else "ok      ") + name)
    if failed:
        bad = True
        print(r.stdout[-3000:], r.stderr[-3000:])
sys.exit(1 if bad else 0)
