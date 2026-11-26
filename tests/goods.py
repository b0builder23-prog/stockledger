import os, sys, tempfile, html, io, datetime as dt
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
    d = dict(d or {}); d["csrf"] = tok(); return c.post(u, data=d, follow_redirects=k.get("follow", False), content_type=k.get("ct"))
B = lambda r: html.unescape(r.get_data(as_text=True))
post("/setup", {"username":"o","password":"password123","password2":"password123"}); post("/login", {"username":"o","password":"password123"})
post("/products/new", {"name":"Laptop","category":"Laptop","tracks_serials":"on","sale_price":"300"})
db = connect(app.config["DB_PATH"]); pid = db.execute("select id from products").fetchone()["id"]
today = dt.date.today().isoformat()
r = post("/stock/add", {"product_id":pid,"purchase_date":today,"unit_cost":"100","extra_cost":"10.00","serials":"A1\nA2\nA3","receipt":(io.BytesIO(b"%PDF-1.4 x"),"inv.pdf")}, follow=True, ct="multipart/form-data")
costs = [r["unit_cost"] for r in db.execute("select unit_cost from stock_lots order by id")]
print("costs (pence):", costs, "sum", sum(costs))
ok1 = sum(costs) == 31000 and sorted(costs) == [10333, 10333, 10334]
lot = db.execute("select id, receipt from stock_lots").fetchone()
ok2 = bool(lot["receipt"]) and c.get(f"/lot/{lot['id']}/receipt").status_code == 200
page = B(c.get("/expenses"))
ok3 = "Goods bought for resale (not already" not in page and "Stock &rarr; Add stock" in page or "Add stock" in page
# legacy goods expense (old data) triggers the warning rather than silently doubling
db.execute("insert into expenses(expense_date,category,description,amount) values(?,?,?,?)",(today,"goods","old entry",30000)); db.commit()
t = B(c.get("/reports/tax"))
ok4 = "Possible double counting" in t
db.execute("delete from expenses"); db.commit()
ok5 = "Possible double counting" not in B(c.get("/reports/tax"))
print(all([ok1,ok2,ok3,ok4,ok5]) and "PASS" or f"FAIL {[ok1,ok2,ok3,ok4,ok5]}")
# upgrade path: an older DB without the receipt column
import sqlite3
p = tmp+"/old.db"; o = sqlite3.connect(p)
o.executescript("create table stock_lots(id integer primary key, product_id int, serial text, qty_initial int, qty_remaining int, unit_cost int, list_price int, purchase_date text, supplier text, condition text, notes text, created_at text);"); o.commit(); o.close()
from stockledger.db import init_db
init_db(p); print("PASS upgrade" if "receipt" in [r[1] for r in sqlite3.connect(p).execute("pragma table_info(stock_lots)")] else "FAIL upgrade")
