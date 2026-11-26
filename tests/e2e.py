import io, os, re, sys, tempfile, zipfile, datetime as dt

tmp = tempfile.mkdtemp(prefix="sl-test-")
os.environ.update(DATA_DIR=tmp + "/data", BACKUP_DIR=tmp + "/backups", NO_BACKUP_THREAD="1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image
from stockledger import app
from stockledger.db import connect

c = app.test_client()
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


def csrf():
    with c.session_transaction() as s:
        if "csrf" not in s:
            c.get("/login")
    with c.session_transaction() as s:
        return s["csrf"]


def post(url, data=None, **kw):
    d = dict(data or {})
    d["csrf"] = csrf()
    return c.post(url, data=d, follow_redirects=kw.pop("follow", False), **kw)


def db():
    return connect(app.config["DB_PATH"])


import html
def body(r):
    return html.unescape(r.get_data(as_text=True))


# ---- auth gate
r = c.get("/"); check("redirects to setup when no users", r.status_code == 302 and "/setup" in r.location)
r = c.post("/setup", data={"username": "a", "password": "x"}); check("setup without csrf rejected", r.status_code == 400)
r = post("/setup", {"username": "owen", "password": "short", "password2": "short"}, follow=True)
check("short password rejected", "at least 8" in body(r))
r = post("/setup", {"username": "owen", "password": "correct horse", "password2": "correct horse", "business_name": "Owen IT"}, follow=True)
check("setup ok", "Account created" in body(r))
check("setup unreachable after first user", c.get("/setup").status_code in (302, 404))
r = c.get("/"); check("login required", r.status_code == 302 and "/login" in r.location)
r = post("/login", {"username": "owen", "password": "wrong"}); check("bad login", "Wrong username" in body(r))
r = post("/login", {"username": "owen", "password": "correct horse"}); check("login ok", r.status_code == 302)
r = c.get("/"); check("dashboard loads", r.status_code == 200 and "Owen IT" in body(r), body(r)[:200])
r = c.get("/login?next=//evil.com");

# ---- settings + logo
buf = io.BytesIO(); Image.new("RGB", (400, 120), (30, 80, 200)).save(buf, "PNG"); buf.seek(0)
r = post("/settings/logo", {"logo": (buf, "logo.png")}, content_type="multipart/form-data", follow=True)
check("logo upload", "Logo updated" in body(r), body(r)[:300])
check("logo served", c.get("/logo").status_code == 200)
r = post("/settings/business", {
    "business_name": "Owen IT", "business_address": "1 High Street\nLondon\nN1 1AA", "business_email": "owen@example.com",
    "business_phone": "07000 000000", "bank_details": "Owen IT\nSort 00-00-00\nAcc 12345678", "invoice_prefix": "INV-",
    "quote_prefix": "QUO-", "next_invoice_no": "1", "next_quote_no": "1", "payment_terms_days": "14",
    "quote_valid_days": "30", "invoice_footer": "Thanks!", "quote_footer": "Subject to stock.", "stock_method": "purchases"},
    follow=True)
check("settings saved", "Settings saved" in body(r))

# ---- products + stock
post("/products/new", {"name": "Dell Latitude 7420", "category": "Laptop", "tracks_serials": "on", "sale_price": "350.00"})
post("/products/new", {"name": "TP-Link Router", "category": "Network equipment", "tracks_serials": "on", "sale_price": "90"})
post("/products/new", {"name": "Cat6 Cable 2m", "category": "Accessory", "sale_price": "5"})
d = db(); P = {r["name"]: r["id"] for r in d.execute("SELECT id, name FROM products")}
check("3 products", len(P) == 3)
today = dt.date.today().isoformat()
r = post("/stock/add", {"product_id": P["Dell Latitude 7420"], "purchase_date": today, "unit_cost": "200", "supplier": "eBay",
                        "serials": "LAP001\nLAP002\nlap003\n"}, follow=True)
check("3 laptops added", "Added 3" in body(r), body(r)[:300])
r = post("/stock/add", {"product_id": P["Dell Latitude 7420"], "purchase_date": today, "unit_cost": "200", "serials": "LAP002"}, follow=True)
check("duplicate serial (any case) rejected", "already exist" in body(r))
post("/stock/add", {"product_id": P["TP-Link Router"], "purchase_date": today, "unit_cost": "50", "serials": "RT-1, RT-2"})
post("/stock/add", {"product_id": P["Cat6 Cable 2m"], "purchase_date": today, "unit_cost": "2", "qty": "10"})
post("/stock/add", {"product_id": P["Cat6 Cable 2m"], "purchase_date": today, "unit_cost": "3", "qty": "5"})
r = c.get("/products?q=LAP002"); check("scanning serial jumps to unit", r.status_code == 302 and "/lot/" in r.location)
r = c.get("/products?q=Dell"); check("product search", "Dell Latitude" in body(r))
r = c.get(f"/products/{P['Dell Latitude 7420']}"); check("product page shows margin", "LAP001" in body(r) and "42.9%" in body(r), "")

# ---- customer + quote -> invoice
post("/customers/new", {"name": "Acme Ltd", "address": "5 Road\nLeeds", "email": "bill@acme.test"})
cid = d.execute("SELECT id FROM customers").fetchone()["id"]
r = post("/new/quote"); qid = int(r.location.rsplit("/", 1)[1])
post(f"/doc/{qid}/header", {"customer_id": cid, "issue_date": today, "due_date": "", "notes": "Quote notes"})
post(f"/doc/{qid}/lines", {"product_id": P["Dell Latitude 7420"], "description": "Dell Latitude 7420", "qty": "2", "unit_price": "350"})
post(f"/doc/{qid}/lines", {"product_id": P["Cat6 Cable 2m"], "qty": "3"})
r = c.get(f"/doc/{qid}/pdf"); check("quote pdf", r.data[:4] == b"%PDF")
open(tmp + "/quote.pdf", "wb").write(r.data)
r = post(f"/doc/{qid}/convert", follow=True)
check("quote converted to draft invoice", "Draft invoice created" in body(r), body(r)[:300])
iid = int(re.search(r"/doc/(\d+)", r.request.path).group(1))
r = post(f"/doc/{iid}/issue", follow=True)
check("cannot issue with unpicked serials", "choose serial numbers" in body(r))
line = d.execute("SELECT dl.id FROM document_lines dl JOIN products p ON p.id=dl.product_id WHERE dl.document_id=? AND p.tracks_serials=1", (iid,)).fetchone()["id"]
lots = {r["serial"]: r["id"] for r in db().execute("SELECT id, serial FROM stock_lots WHERE serial IS NOT NULL")}
r = post(f"/line/{line}/units", {"lot_ids": [lots["LAP001"], lots["LAP002"]]}, follow=True)
check("serials chosen", "LAP001" in body(r) and "LAP002" in body(r))
# free-text delivery line
post(f"/doc/{iid}/lines", {"description": "Courier delivery", "qty": "1", "unit_price": "10"})
check("bulk stock reserved on draft", db().execute("SELECT SUM(qty_remaining) n FROM stock_lots WHERE serial IS NULL").fetchone()["n"] == 12)
r = post(f"/doc/{iid}/issue", follow=True)
check("invoice issued", "Invoice issued" in body(r) and "INV-0001" in body(r), body(r)[:300])
check("issued invoice total 725", "£725.00" in body(r))
check("FIFO cost: 3 cables = 3x£2", db().execute("""SELECT SUM(lu.qty*l.unit_cost) c FROM line_units lu JOIN stock_lots l ON l.id=lu.lot_id
        JOIN document_lines dl ON dl.id=lu.line_id JOIN products p ON p.id=dl.product_id WHERE dl.document_id=? AND p.tracks_serials=0""", (iid,)).fetchone()["c"] == 600)
r = post(f"/doc/{iid}/lines", {"description": "x"}); check("cannot edit issued invoice", r.status_code == 400)
r = post(f"/doc/{iid}/payments", {"amount": "300", "paid_date": today, "method": "Bank transfer"}, follow=True)
check("part payment", "Payment recorded" in body(r) and "Part paid" in body(r))
r = post(f"/doc/{iid}/payments", {"amount": "999"}, follow=True); check("overpayment blocked", "more than the balance" in body(r))
r = post(f"/doc/{iid}/payments", {"amount": "425", "paid_date": today}, follow=True); check("paid in full", "Paid" in body(r))
r = c.get(f"/doc/{iid}/pdf"); pdf = r.data; check("invoice pdf", pdf[:4] == b"%PDF")
open(tmp + "/invoice.pdf", "wb").write(pdf)
r = c.get(f"/products/{P['Dell Latitude 7420']}?show=all")
check("sold unit shows invoice + 150 profit", "Sold INV-0001" in body(r) and "£150.00" in body(r))
r = c.get(f"/lot/{lots['LAP001']}"); check("lot cost locked once sold", "readonly" in body(r))
r = post(f"/lot/{lots['LAP001']}/delete", follow=True); check("sold unit can't be deleted", "can't be deleted" in body(r))

# ---- voiding returns stock
r = post("/new/invoice"); jid = int(r.location.rsplit("/", 1)[1])
post(f"/doc/{jid}/header", {"customer_id": cid, "issue_date": today})
post(f"/doc/{jid}/lines", {"product_id": P["Cat6 Cable 2m"], "qty": "99"}, follow=True)
check("can't oversell bulk stock", db().execute("SELECT COUNT(*) n FROM document_lines WHERE document_id=?", (jid,)).fetchone()["n"] == 0)
post(f"/doc/{jid}/lines", {"product_id": P["TP-Link Router"], "qty": "1", "unit_price": "90"})
l2 = db().execute("SELECT id FROM document_lines WHERE document_id=?", (jid,)).fetchone()["id"]
post(f"/line/{l2}/units", {"lot_ids": [lots["RT-1"]]})
post(f"/doc/{jid}/issue")
post(f"/doc/{jid}/payments", {"amount": "90", "paid_date": today})
r = post(f"/doc/{jid}/void", follow=True); check("void blocked while paid", "Delete the payments" in body(r))
pid = db().execute("SELECT id FROM payments WHERE document_id=?", (jid,)).fetchone()["id"]
post(f"/payments/{pid}/delete"); post(f"/doc/{jid}/void")
check("void returns serial to stock", db().execute("SELECT qty_remaining q FROM stock_lots WHERE serial='RT-1'").fetchone()["q"] == 1)

# ---- expenses
buf = io.BytesIO(b"%PDF-1.4 fake receipt")
r = post("/expenses", {"expense_date": today, "category": "office", "amount": "50", "description": "Printer ink", "supplier": "Staples",
                       "method": "Card", "receipt": (buf, "r.pdf")}, content_type="multipart/form-data", follow=True)
check("expense added", "Expense added" in body(r) and "Printer ink" in body(r))
eid = db().execute("SELECT id FROM expenses").fetchone()["id"]
check("receipt retrievable", c.get(f"/receipt/{eid}").status_code == 200)
r = post("/expenses", {"expense_date": today, "category": "office", "amount": "1", "description": "bad", "receipt": (io.BytesIO(b"x"), "x.exe")},
         content_type="multipart/form-data", follow=True)
check("exe receipt rejected", "must be a PDF or an image" in body(r))
post("/income", {"income_date": today, "description": "Cash sale", "amount": "20"})

# ---- reports: expected turnover 725+20=745 ; expenses = stock bought (3*200+2*50+10*2+5*3=735) + 50 = 785 ; net = -40
r = c.get("/reports/tax"); t = body(r)
check("tax: turnover £745.00", "£745.00" in t)
check("tax: expenses £785.00", "£785.00" in t, re.sub(r"<[^>]+>", " ", t)[:900])
check("tax: net -£40.00", "-£40.00" in t)
r = c.get("/reports/tax?csv=1"); check("tax csv", "TOTAL TURNOVER,745.00" in body(r) and "NET PROFIT,-40.00" in body(r), body(r))
for p in ("week", "month", "year", "taxyear"):
    r = c.get(f"/reports?period={p}"); check(f"sales report {p}", r.status_code == 200 and "£725.00" in body(r))
r = c.get("/reports?period=month&csv=1"); check("sales csv gross profit 319", "319.00" in body(r), body(r))
for w in ("stock", "income", "expenses", "invoices"):
    check(f"export {w}", c.get(f"/export/{w}.csv").status_code == 200)
check("dashboard", c.get("/").status_code == 200)
for u in ("/products", "/customers", "/quotes", "/invoices", "/expenses", "/income", "/settings", f"/doc/{iid}", f"/doc/{qid}", f"/customers/{cid}/edit",
          f"/expenses/{eid}", f"/products/{P['Cat6 Cable 2m']}", f"/products/{P['Cat6 Cable 2m']}/edit", "/stock/add", "/invoices?status=paid"):
    rr = c.get(u); check(f"GET {u}", rr.status_code == 200, rr.status_code)

# ---- backup
r = post("/backup/now", follow=True); check("backup now", "Backup written" in body(r))
name = [f for f in os.listdir(tmp + "/backups")][0]
z = zipfile.ZipFile(tmp + "/backups/" + name); names = z.namelist()
check("backup has db + logo + receipt", "stockledger.db" in names and any("logo-" in n for n in names) and any("receipts/" in n for n in names), names)
z.extract("stockledger.db", tmp + "/restore")
import sqlite3
check("backup db is readable", sqlite3.connect(tmp + "/restore/stockledger.db").execute("SELECT COUNT(*) FROM stock_lots").fetchone()[0] == 7)
check("backup download", c.get("/backup/" + name).status_code == 200)
check("backup path traversal blocked", c.get("/backup/..%2F..%2Fetc%2Fpasswd").status_code == 404)

# ---- logout
post("/logout"); check("logged out", c.get("/").status_code == 302)
print("\nTMP", tmp)
print("FAILED:", fails if fails else "none")
sys.exit(1 if fails else 0)
