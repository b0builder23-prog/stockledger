import os, sys, tempfile, html, datetime as dt
tmp = tempfile.mkdtemp()
os.environ.update(DATA_DIR=tmp+"/d", BACKUP_DIR=tmp+"/b", NO_BACKUP_THREAD="1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stockledger import app
from stockledger.db import connect
c = app.test_client()
def tok():
    with c.session_transaction() as s:
        if "csrf" not in s: c.get("/login")
    with c.session_transaction() as s: return s["csrf"]
def post(u, d=None, **k):
    d = dict(d or {}); d["csrf"] = tok(); return c.post(u, data=d, follow_redirects=k.get("follow", False))
post("/setup", {"username":"o","password":"password123","password2":"password123"})
post("/login", {"username":"o","password":"password123"})
post("/products/new", {"name":"Laptop","category":"Laptop","tracks_serials":"on","sale_price":"300"})
db = connect(app.config["DB_PATH"])
pid = db.execute("select id from products").fetchone()["id"]
today = dt.date.today().isoformat()
post("/stock/add", {"product_id":pid,"purchase_date":today,"unit_cost":"100","serials":"A1\nA2"})
post("/customers/new", {"name":"Bob"})
cid = db.execute("select id from customers").fetchone()["id"]
r = post("/new/invoice"); iid = int(r.location.rsplit("/",1)[1])
post(f"/doc/{iid}/lines", {"product_id":pid})
line = db.execute("select id from document_lines").fetchone()["id"]
lot = db.execute("select id from stock_lots where serial='A1'").fetchone()["id"]
post(f"/line/{line}/units", {"lot_ids":[lot]})
# the user's scenario: customer chosen, never pressed Save, press Issue straight away
r = post(f"/doc/{iid}/header", {"customer_id":cid,"issue_date":today,"due_date":"","notes":"","then":"issue"}, follow=True)
t = html.unescape(r.get_data(as_text=True))
st = db.execute("select status, number, customer_id from documents where id=?", (iid,)).fetchone()
print("status:", dict(st))
print("PASS" if st["status"]=="issued" and st["customer_id"]==cid and "Invoice issued" in t else "FAIL")
# and issuing with no customer still gives a clear message, not a crash
r = post("/new/invoice"); j = int(r.location.rsplit("/",1)[1])
post(f"/doc/{j}/lines", {"description":"Delivery","unit_price":"5"})
r = post(f"/doc/{j}/header", {"customer_id":"","then":"issue"}, follow=True)
print("PASS" if "choose a customer" in html.unescape(r.get_data(as_text=True)) else "FAIL")
