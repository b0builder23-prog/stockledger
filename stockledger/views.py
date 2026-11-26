import csv
import datetime as dt
import hmac
import io
import os
import re
import secrets
import sqlite3
import uuid

from flask import (Response, abort, flash, g, redirect, render_template, request, send_file, session, url_for)
from markupsafe import Markup
from PIL import Image
from werkzeug.security import check_password_hash, generate_password_hash

from . import app
from .backup import NAME_RE, list_backups, make_backup
from .db import get_db, get_settings, set_setting
from .pdfgen import build_pdf
from .util import (money, parse_date, pct, pence_input, period_key, tax_year_label, tax_year_of, tax_year_range,
                   to_pence)

PRODUCT_CATEGORIES = ["Laptop", "Network equipment", "Printer", "Accessory", "Other"]
SERIAL_BY_DEFAULT = {"Laptop", "Network equipment", "Printer"}
PAY_METHODS = ["Bank transfer", "Card", "Cash", "PayPal", "Other"]

# (key, label, heading on the HMRC self-employment pages)
EXPENSE_CATEGORIES = [
    ("goods", "Goods bought for resale (not already in stock records)", "Cost of goods bought for resale or goods used"),
    ("staff", "Wages, salaries and other staff costs", "Wages, salaries and other staff costs"),
    ("travel", "Car, van and travel expenses", "Car, van and travel expenses"),
    ("premises", "Rent, rates, power and insurance", "Rent, rates, power and insurance costs"),
    ("repairs", "Repairs and maintenance of equipment", "Repairs and maintenance of property and equipment"),
    ("office", "Phone, internet, stationery and postage", "Phone, fax, stationery and other office costs"),
    ("advertising", "Advertising and marketing", "Advertising and business entertainment costs"),
    ("interest", "Interest on business loans", "Interest on bank and other loans"),
    ("bank", "Bank, card and payment processing fees", "Bank, credit card and other financial charges"),
    ("professional", "Accountancy, legal and professional fees", "Accountancy, legal and other professional fees"),
    ("equipment", "Equipment and tools (not for resale)", "Other business expenses"),
    ("other", "Other business expenses", "Other business expenses"),
]
CAT_LABEL = {k: l for k, l, _h in EXPENSE_CATEGORIES}
CAT_HEADING = {k: h for k, _l, h in EXPENSE_CATEGORIES}
HEADING_ORDER = []
for _k, _l, _h in EXPENSE_CATEGORIES:
    if _h not in HEADING_ORDER:
        HEADING_ORDER.append(_h)
GOODS_HEADING = CAT_HEADING["goods"]


# --------------------------------------------------------------------------- helpers
def q(sql, *args):
    return get_db().execute(sql, args).fetchall()


def q1(sql, *args):
    return get_db().execute(sql, args).fetchone()


def x(sql, *args):
    return get_db().execute(sql, args)


def today():
    return dt.date.today()


def form_int(name, default=None, min_value=None):
    raw = (request.form.get(name) or "").strip()
    if raw == "":
        if default is None:
            raise ValueError(f"Please enter a number for {name.replace('_', ' ')}")
        return default
    try:
        val = int(raw)
    except ValueError:
        raise ValueError(f"'{raw}' is not a whole number")
    if min_value is not None and val < min_value:
        raise ValueError(f"{name.replace('_', ' ').capitalize()} must be at least {min_value}")
    return val


def parse_serials(text):
    seen, out = set(), []
    for part in re.split(r"[\n\r,;\t]+", text or ""):
        s = part.strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return dict(
        csrf_input=lambda: Markup(f'<input type="hidden" name="csrf" value="{session["csrf"]}">'),
        money=money, pence=pence_input, pct=pct, today_iso=today().isoformat(),
        S=get_settings(), CAT_LABEL=CAT_LABEL, EXPENSE_CATEGORIES=EXPENSE_CATEGORIES,
        PAY_METHODS=PAY_METHODS, PRODUCT_CATEGORIES=PRODUCT_CATEGORIES, tax_year_label=tax_year_label,
        endpoint=request.endpoint or "", user=getattr(g, "user", None),
    )


@app.template_filter("dmy")
def dmy(iso):
    if not iso:
        return ""
    try:
        return dt.date.fromisoformat(iso).strftime("%d %b %Y")
    except ValueError:
        return iso


@app.template_filter("filesize")
def filesize(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# --------------------------------------------------------------------------- auth + csrf
_FAILS = {}  # ip -> (count, locked_until)


@app.before_request
def gate():
    ep = request.endpoint
    if ep in ("static", "healthz"):
        return None
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    no_users = q1("SELECT 1 FROM users LIMIT 1") is None
    if no_users:
        if ep != "setup":
            return redirect(url_for("setup"))
    else:
        g.user = None
        if "uid" in session:
            g.user = q1("SELECT id, username FROM users WHERE id=?", session["uid"])
        if g.user is None and ep not in ("login",):
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        if not hmac.compare_digest(sent, session.get("csrf", "\0")):
            abort(400, "Security token missing or expired - go back, refresh the page and try again.")
    return None


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if q1("SELECT 1 FROM users LIMIT 1"):
        abort(404)
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        pw = request.form.get("password", "")
        if not username or len(pw) < 8:
            flash("Choose a username and a password of at least 8 characters.", "error")
        elif pw != request.form.get("password2"):
            flash("The two passwords don't match.", "error")
        else:
            x("INSERT INTO users(username, password_hash) VALUES(?,?)", username, generate_password_hash(pw))
            name = request.form.get("business_name", "").strip()
            if name:
                set_setting("business_name", name)
            get_db().commit()
            flash("Account created - please sign in.", "ok")
            return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr or "?"
    if request.method == "POST":
        count, locked = _FAILS.get(ip, (0, 0))
        if locked > dt.datetime.now().timestamp():
            flash("Too many attempts - wait a few minutes and try again.", "error")
            return render_template("login.html"), 429
        row = q1("SELECT * FROM users WHERE username=?", request.form.get("username", "").strip())
        if row and check_password_hash(row["password_hash"], request.form.get("password", "")):
            _FAILS.pop(ip, None)
            keep = session.get("csrf")
            session.clear()
            session["csrf"] = keep or secrets.token_hex(16)
            session["uid"] = row["id"]
            session.permanent = True
            nxt = request.args.get("next", "")
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("dashboard"))
        count += 1
        _FAILS[ip] = (count, dt.datetime.now().timestamp() + 300 if count >= 5 else 0)
        flash("Wrong username or password.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- documents: shared queries
DOC_SQL = """
SELECT d.*, c.name AS customer_name,
  COALESCE((SELECT SUM(qty*unit_price) FROM document_lines WHERE document_id=d.id),0) AS total,
  COALESCE((SELECT SUM(amount) FROM payments WHERE document_id=d.id),0) AS paid
FROM documents d LEFT JOIN customers c ON c.id=d.customer_id
"""


def pay_status(d):
    """Human status for an invoice row from DOC_SQL."""
    if d["kind"] != "invoice":
        return d["status"].capitalize()
    if d["status"] == "draft":
        return "Draft"
    if d["status"] == "void":
        return "Void"
    if d["total"] <= 0 or d["paid"] >= d["total"]:
        return "Paid"
    if d["paid"] > 0:
        return "Part paid"
    if d["due_date"] and d["due_date"] < today().isoformat():
        return "Overdue"
    return "Unpaid"


def get_doc(doc_id):
    d = q1(DOC_SQL + " WHERE d.id=?", doc_id)
    if d is None:
        abort(404)
    return d


def is_editable(d):
    return (d["kind"] == "invoice" and d["status"] == "draft") or (d["kind"] == "quote" and d["status"] == "open")


def doc_lines(doc_id):
    out = []
    rows = q("""SELECT dl.*, p.tracks_serials, p.name AS product_name FROM document_lines dl
                LEFT JOIN products p ON p.id=dl.product_id WHERE dl.document_id=? ORDER BY dl.position, dl.id""", doc_id)
    for r in rows:
        ln = dict(r)
        units = q("""SELECT lu.qty, l.serial, l.unit_cost, l.id AS lot_id FROM line_units lu
                     JOIN stock_lots l ON l.id=lu.lot_id WHERE lu.line_id=? ORDER BY l.serial, l.id""", r["id"])
        ln["units"] = units
        ln["serials"] = [u["serial"] for u in units if u["serial"]]
        ln["alloc_qty"] = sum(u["qty"] for u in units)
        ln["cost"] = sum(u["qty"] * u["unit_cost"] for u in units)
        ln["amount"] = r["qty"] * r["unit_price"]
        ln["needs_units"] = bool(r["product_id"]) and r["tracks_serials"] is not None and ln["alloc_qty"] != r["qty"]
        out.append(ln)
    return out


def release_line(line_id):
    for u in q("SELECT lot_id, qty FROM line_units WHERE line_id=?", line_id):
        x("UPDATE stock_lots SET qty_remaining = qty_remaining + ? WHERE id=?", u["qty"], u["lot_id"])
    x("DELETE FROM line_units WHERE line_id=?", line_id)


def release_document(doc_id):
    for ln in q("SELECT id FROM document_lines WHERE document_id=?", doc_id):
        release_line(ln["id"])


def allocate_bulk(line_id, product_id, qty):
    """Take qty from non-serial lots, oldest first.  Raises ValueError when stock is short."""
    lots = q("""SELECT id, qty_remaining FROM stock_lots
                WHERE product_id=? AND serial IS NULL AND qty_remaining>0 ORDER BY purchase_date, id""", product_id)
    available = sum(l["qty_remaining"] for l in lots)
    if available < qty:
        raise ValueError(f"Only {available} in stock (you asked for {qty}).")
    need = qty
    for lot in lots:
        take = min(need, lot["qty_remaining"])
        x("UPDATE stock_lots SET qty_remaining = qty_remaining - ? WHERE id=?", take, lot["id"])
        x("INSERT INTO line_units(line_id, lot_id, qty) VALUES(?,?,?)", line_id, lot["id"], take)
        need -= take
        if need == 0:
            break


def next_number(kind):
    prefix_key, counter_key = (("invoice_prefix", "next_invoice_no") if kind == "invoice"
                               else ("quote_prefix", "next_quote_no"))
    s = get_settings()
    n = int(s[counter_key])
    while True:
        number = f"{s[prefix_key]}{n:04d}"
        if q1("SELECT 1 FROM documents WHERE kind=? AND number=?", kind, number) is None:
            break
        n += 1
    set_setting(counter_key, str(n + 1))
    return number


# --------------------------------------------------------------------------- reporting helpers
def tax_summary(start_year):
    start, end = tax_year_range(start_year)
    a, b = start.isoformat(), end.isoformat()
    received = q1("""SELECT COALESCE(SUM(p.amount),0) AS v FROM payments p JOIN documents d ON d.id=p.document_id
                     WHERE d.status='issued' AND p.paid_date BETWEEN ? AND ?""", a, b)["v"]
    other = q1("SELECT COALESCE(SUM(amount),0) AS v FROM other_income WHERE income_date BETWEEN ? AND ?", a, b)["v"]
    invoiced = q1("""SELECT COALESCE(SUM(dl.qty*dl.unit_price),0) AS v FROM document_lines dl
                     JOIN documents d ON d.id=dl.document_id
                     WHERE d.kind='invoice' AND d.status='issued' AND d.issue_date BETWEEN ? AND ?""", a, b)["v"]
    by_cat = {r["category"]: r["v"] for r in q(
        "SELECT category, SUM(amount) AS v FROM expenses WHERE expense_date BETWEEN ? AND ? GROUP BY category", a, b)}
    purchases = q1("""SELECT COALESCE(SUM(qty_initial*unit_cost),0) AS v FROM stock_lots
                      WHERE purchase_date BETWEEN ? AND ?""", a, b)["v"]
    cogs = q1("""SELECT COALESCE(SUM(lu.qty*l.unit_cost),0) AS v FROM line_units lu
                 JOIN stock_lots l ON l.id=lu.lot_id JOIN document_lines dl ON dl.id=lu.line_id
                 JOIN documents d ON d.id=dl.document_id
                 WHERE d.kind='invoice' AND d.status='issued' AND d.issue_date BETWEEN ? AND ?""", a, b)["v"]
    method = get_settings().get("stock_method", "purchases")
    stock_cost = purchases if method == "purchases" else cogs

    by_heading = {h: {"total": 0, "items": []} for h in HEADING_ORDER}
    by_heading[GOODS_HEADING]["items"].append(
        ("Stock bought (from stock records)" if method == "purchases" else "Cost of stock sold (from stock records)",
         stock_cost))
    by_heading[GOODS_HEADING]["total"] += stock_cost
    for key, label, heading in EXPENSE_CATEGORIES:
        amount = by_cat.get(key, 0)
        if amount:
            by_heading[heading]["items"].append((label, amount))
            by_heading[heading]["total"] += amount
    expenses_total = sum(h["total"] for h in by_heading.values())
    turnover = received + other
    return dict(
        year=start_year, label=tax_year_label(start_year), start=start, end=end, received=received, other=other,
        turnover=turnover, invoiced=invoiced, expenses_total=expenses_total, net=turnover - expenses_total,
        headings=[(h, by_heading[h]) for h in HEADING_ORDER if by_heading[h]["total"] or by_heading[h]["items"]],
        method=method, purchases=purchases, cogs=cogs, goods_manual=by_cat.get("goods", 0),
    )


def tax_years_available():
    dates = []
    for sql in ("SELECT MIN(paid_date) AS d FROM payments", "SELECT MIN(expense_date) AS d FROM expenses",
                "SELECT MIN(purchase_date) AS d FROM stock_lots", "SELECT MIN(income_date) AS d FROM other_income",
                "SELECT MIN(issue_date) AS d FROM documents WHERE kind='invoice'"):
        v = q1(sql)["d"]
        if v:
            dates.append(dt.date.fromisoformat(v))
    current = tax_year_of(today())
    first = min([tax_year_of(d) for d in dates] + [current])
    return list(range(current, first - 1, -1))


def sales_table(period):
    inv = q("""SELECT d.issue_date,
          COALESCE((SELECT SUM(qty*unit_price) FROM document_lines WHERE document_id=d.id),0) AS total,
          COALESCE((SELECT SUM(lu.qty*l.unit_cost) FROM line_units lu JOIN document_lines dl ON dl.id=lu.line_id
                    JOIN stock_lots l ON l.id=lu.lot_id WHERE dl.document_id=d.id),0) AS cost
        FROM documents d WHERE d.kind='invoice' AND d.status='issued' AND d.issue_date IS NOT NULL""")
    pays = q("""SELECT p.paid_date, p.amount FROM payments p JOIN documents d ON d.id=p.document_id
                WHERE d.status='issued'""")
    other = q("SELECT income_date, amount FROM other_income")
    table = {}

    def row(d):
        key, label = period_key(d, period)
        return table.setdefault(key, dict(label=label, count=0, invoiced=0, cost=0, received=0, other=0))

    for r in inv:
        t = row(dt.date.fromisoformat(r["issue_date"]))
        t["count"] += 1
        t["invoiced"] += r["total"]
        t["cost"] += r["cost"]
    for r in pays:
        row(dt.date.fromisoformat(r["paid_date"]))["received"] += r["amount"]
    for r in other:
        row(dt.date.fromisoformat(r["income_date"]))["other"] += r["amount"]
    rows = []
    for key in sorted(table, reverse=True):
        t = table[key]
        t["profit"] = t["invoiced"] - t["cost"]
        t["margin"] = pct(t["profit"], t["invoiced"])
        rows.append(t)
    return rows


def csv_response(filename, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def pick_ty(raw):
    """Tax-year filter from the query string: 'all', or a start year (defaults to the current tax year)."""
    raw = (raw or "").strip()
    if raw == "all":
        return "all"
    if raw.isdigit():
        return raw
    return str(tax_year_of(today()))


def gbp(pence):
    return pence_input(pence)


# --------------------------------------------------------------------------- dashboard
@app.route("/")
def dashboard():
    stock = q1("""SELECT COALESCE(SUM(l.qty_remaining),0) AS n, COALESCE(SUM(l.qty_remaining*l.unit_cost),0) AS cost,
                  COALESCE(SUM(l.qty_remaining*COALESCE(l.list_price,p.sale_price)),0) AS retail
                  FROM stock_lots l JOIN products p ON p.id=l.product_id""")
    issued = q(DOC_SQL + " WHERE d.kind='invoice' AND d.status='issued' ORDER BY d.issue_date DESC, d.id DESC")
    outstanding = [d for d in issued if d["total"] > d["paid"]]
    overdue = [d for d in outstanding if d["due_date"] and d["due_date"] < today().isoformat()]
    t = today()
    month_key = t.strftime("%Y-%m")
    month = next((r for r in sales_table("month") if r["label"] == t.strftime("%B %Y")), None)
    ty = tax_summary(tax_year_of(t))
    drafts = q1("SELECT COUNT(*) AS n FROM documents WHERE kind='invoice' AND status='draft'")["n"]
    return render_template("dashboard.html", stock=stock, outstanding=outstanding, overdue=overdue,
                           outstanding_total=sum(d["total"] - d["paid"] for d in outstanding), month=month, ty=ty,
                           recent=[dict(d, pay=pay_status(d)) for d in issued[:6]], drafts=drafts, month_key=month_key)


# --------------------------------------------------------------------------- products + stock
@app.route("/products")
def products():
    text = request.args.get("q", "").strip()
    cat = request.args.get("cat", "").strip()
    if text:
        lot = q1("SELECT id FROM stock_lots WHERE serial=? COLLATE NOCASE", text)
        if lot:
            return redirect(url_for("lot_view", lot_id=lot["id"]))
        prod = q1("SELECT id FROM products WHERE barcode=? COLLATE NOCASE AND barcode<>''", text)
        if prod:
            return redirect(url_for("product_view", product_id=prod["id"]))
    sql = """SELECT p.*, COALESCE(SUM(l.qty_remaining),0) AS in_stock,
             COALESCE(SUM(l.qty_remaining*l.unit_cost),0) AS cost_value,
             COALESCE(SUM(l.qty_remaining*COALESCE(l.list_price,p.sale_price)),0) AS retail_value
             FROM products p LEFT JOIN stock_lots l ON l.product_id=p.id WHERE p.active=1"""
    args = []
    if text:
        like = f"%{text}%"
        sql += """ AND (p.name LIKE ? OR p.sku LIKE ? OR p.barcode LIKE ? OR EXISTS(
                   SELECT 1 FROM stock_lots s WHERE s.product_id=p.id AND s.serial LIKE ?))"""
        args += [like] * 4
    if cat:
        sql += " AND p.category=?"
        args.append(cat)
    sql += " GROUP BY p.id ORDER BY p.category, p.name"
    rows = q(sql, *args)
    archived = q("SELECT * FROM products WHERE active=0 ORDER BY name")
    return render_template("products.html", rows=rows, text=text, cat=cat, archived=archived,
                           total_cost=sum(r["cost_value"] for r in rows), total_retail=sum(r["retail_value"] for r in rows))


def _product_form(product=None):
    defaults = dict(name="", sku="", category="Laptop", tracks_serials=1, sale_price="", barcode="", notes="")
    if product:
        defaults = dict(product)
        defaults["sale_price"] = pence_input(product["sale_price"])
    return defaults


@app.route("/products/new", methods=["GET", "POST"])
def product_new():
    f = _product_form()
    if request.method == "POST":
        f = dict(request.form)
        try:
            name = request.form.get("name", "").strip()
            if not name:
                raise ValueError("Give the product a name.")
            cat = request.form.get("category", "Other").strip() or "Other"
            cur = x("""INSERT INTO products(sku,name,category,tracks_serials,sale_price,barcode,notes)
                       VALUES(?,?,?,?,?,?,?)""",
                    request.form.get("sku", "").strip() or None, name, cat, 1 if request.form.get("tracks_serials") else 0,
                    to_pence(request.form.get("sale_price")), request.form.get("barcode", "").strip() or None,
                    request.form.get("notes", "").strip())
            get_db().commit()
            flash("Product added - now add some stock.", "ok")
            return redirect(url_for("stock_add", product_id=cur.lastrowid))
        except ValueError as e:
            flash(str(e), "error")
    return render_template("product_form.html", f=f, product=None)


@app.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
def product_edit(product_id):
    p = q1("SELECT * FROM products WHERE id=?", product_id) or abort(404)
    f = _product_form(p)
    if request.method == "POST":
        f = dict(request.form)
        try:
            name = request.form.get("name", "").strip()
            if not name:
                raise ValueError("Give the product a name.")
            tracks = 1 if request.form.get("tracks_serials") else 0
            if tracks != p["tracks_serials"] and q1("SELECT 1 FROM stock_lots WHERE product_id=?", product_id):
                raise ValueError("You can't switch serial tracking once stock exists for this product.")
            x("""UPDATE products SET sku=?, name=?, category=?, tracks_serials=?, sale_price=?, barcode=?, notes=?
                 WHERE id=?""",
              request.form.get("sku", "").strip() or None, name, request.form.get("category", "Other").strip() or "Other",
              tracks, to_pence(request.form.get("sale_price")), request.form.get("barcode", "").strip() or None,
              request.form.get("notes", "").strip(), product_id)
            get_db().commit()
            flash("Saved.", "ok")
            return redirect(url_for("product_view", product_id=product_id))
        except ValueError as e:
            flash(str(e), "error")
    return render_template("product_form.html", f=f, product=p)


@app.route("/products/<int:product_id>/archive", methods=["POST"])
def product_archive(product_id):
    p = q1("SELECT * FROM products WHERE id=?", product_id) or abort(404)
    x("UPDATE products SET active=? WHERE id=?", 0 if p["active"] else 1, product_id)
    get_db().commit()
    flash("Product archived." if p["active"] else "Product restored.", "ok")
    return redirect(url_for("products"))


@app.route("/products/<int:product_id>")
def product_view(product_id):
    p = q1("SELECT * FROM products WHERE id=?", product_id) or abort(404)
    show = request.args.get("show", "stock")
    serial_lots = q("""SELECT l.*, dl.unit_price AS sale_price, d.id AS doc_id, d.number, d.status AS doc_status,
                       d.issue_date AS sold_date, c.name AS customer_name
                       FROM stock_lots l
                       LEFT JOIN line_units lu ON lu.lot_id=l.id
                       LEFT JOIN document_lines dl ON dl.id=lu.line_id
                       LEFT JOIN documents d ON d.id=dl.document_id
                       LEFT JOIN customers c ON c.id=d.customer_id
                       WHERE l.product_id=? AND l.serial IS NOT NULL ORDER BY l.purchase_date DESC, l.serial""", product_id)
    if show == "stock":
        serial_lots = [l for l in serial_lots if l["doc_id"] is None]
    bulk_lots = q("""SELECT * FROM stock_lots WHERE product_id=? AND serial IS NULL ORDER BY purchase_date DESC, id DESC""",
                  product_id)
    sold_count = q1("""SELECT COUNT(*) AS n FROM stock_lots l JOIN line_units lu ON lu.lot_id=l.id
                       WHERE l.product_id=? AND l.serial IS NOT NULL""", product_id)["n"]
    in_stock = q1("SELECT COALESCE(SUM(qty_remaining),0) AS n FROM stock_lots WHERE product_id=?", product_id)["n"]
    return render_template("product_view.html", p=p, serial_lots=serial_lots, bulk_lots=bulk_lots, show=show,
                           sold_count=sold_count, in_stock=in_stock)


@app.route("/stock/add", methods=["GET", "POST"])
def stock_add():
    prods = q("SELECT * FROM products WHERE active=1 ORDER BY category, name")
    if not prods:
        flash("Add a product first, then you can add stock to it.", "error")
        return redirect(url_for("product_new"))
    f = dict(product_id=request.args.get("product_id", ""), purchase_date=today().isoformat(), supplier="",
             condition="", unit_cost="", extra_cost="", list_price="", serials="", qty="1", notes="")
    if request.method == "POST":
        f.update(request.form)
        try:
            p = q1("SELECT * FROM products WHERE id=?", request.form.get("product_id")) or abort(400)
            pdate = parse_date(request.form.get("purchase_date"), today()).isoformat()
            cost = to_pence(request.form.get("unit_cost"))
            extra = to_pence(request.form.get("extra_cost"))  # delivery etc. for the whole batch
            receipt = _save_receipt(request.files.get("receipt"))
            price_raw = request.form.get("list_price", "").strip()
            price = to_pence(price_raw) if price_raw else None
            common = (p["id"], cost, price, pdate, request.form.get("supplier", "").strip(),
                      request.form.get("condition", "").strip(), request.form.get("notes", "").strip())
            if p["tracks_serials"]:
                serials = parse_serials(request.form.get("serials"))
                if not serials:
                    raise ValueError("Enter at least one serial number (one per line).")
                clash = [s for s in serials if q1(
                    "SELECT 1 FROM stock_lots WHERE product_id=? AND serial=? COLLATE NOCASE", p["id"], s)]
                if clash:
                    raise ValueError("These serial numbers already exist for this product: " + ", ".join(clash[:10]))
                base, rem = divmod(extra, len(serials))  # share the extra cost out to the penny
                for i, s in enumerate(serials):
                    x("""INSERT INTO stock_lots(product_id, serial, qty_initial, qty_remaining, unit_cost, list_price,
                         purchase_date, supplier, condition, notes, receipt) VALUES(?,?,1,1,?,?,?,?,?,?,?)""",
                      common[0], s, cost + base + (1 if i < rem else 0), *common[2:], receipt)
                added = f"{len(serials)} unit(s)"
            else:
                qty = form_int("qty", min_value=1)
                x("""INSERT INTO stock_lots(product_id, serial, qty_initial, qty_remaining, unit_cost, list_price,
                     purchase_date, supplier, condition, notes, receipt) VALUES(?,NULL,?,?,?,?,?,?,?,?,?)""",
                  common[0], qty, qty, cost + round(extra / qty), *common[2:], receipt)
                added = f"{qty} unit(s)"
            get_db().commit()
            flash(f"Added {added} of {p['name']}.", "ok")
            return redirect(url_for("product_view", product_id=p["id"]))
        except ValueError as e:
            get_db().rollback()
            flash(str(e), "error")
        except sqlite3.IntegrityError:
            get_db().rollback()
            flash("A serial number in that list already exists.", "error")
    return render_template("stock_add.html", prods=prods, f=f)


@app.route("/lot/<int:lot_id>", methods=["GET", "POST"])
def lot_view(lot_id):
    lot = q1("""SELECT l.*, p.name AS product_name, p.tracks_serials, p.sale_price AS product_price
                FROM stock_lots l JOIN products p ON p.id=l.product_id WHERE l.id=?""", lot_id) or abort(404)
    allocations = q("""SELECT lu.qty, dl.unit_price, d.id AS doc_id, d.number, d.status, d.issue_date, c.name AS customer_name
                       FROM line_units lu JOIN document_lines dl ON dl.id=lu.line_id JOIN documents d ON d.id=dl.document_id
                       LEFT JOIN customers c ON c.id=d.customer_id WHERE lu.lot_id=? ORDER BY d.id""", lot_id)
    locked = bool(allocations)
    if request.method == "POST":
        try:
            price_raw = request.form.get("list_price", "").strip()
            price = to_pence(price_raw) if price_raw else None
            pdate = parse_date(request.form.get("purchase_date"), today()).isoformat()
            serial = lot["serial"]
            if lot["serial"] is not None:
                serial = request.form.get("serial", "").strip()
                if not serial:
                    raise ValueError("A serial number is required.")
            cost = lot["unit_cost"] if locked else to_pence(request.form.get("unit_cost"))
            x("""UPDATE stock_lots SET serial=?, unit_cost=?, list_price=?, purchase_date=?, supplier=?, condition=?, notes=?
                 WHERE id=?""", serial, cost, price, pdate, request.form.get("supplier", "").strip(),
              request.form.get("condition", "").strip(), request.form.get("notes", "").strip(), lot_id)
            get_db().commit()
            flash("Saved.", "ok")
            return redirect(url_for("lot_view", lot_id=lot_id))
        except ValueError as e:
            flash(str(e), "error")
        except sqlite3.IntegrityError:
            get_db().rollback()
            flash("Another unit of this product already has that serial number.", "error")
    return render_template("lot_view.html", lot=lot, allocations=allocations, locked=locked)


@app.route("/lot/<int:lot_id>/receipt")
def lot_receipt(lot_id):
    lot = q1("SELECT receipt FROM stock_lots WHERE id=?", lot_id)
    if not lot or not lot["receipt"]:
        abort(404)
    return send_file(os.path.join(app.config["UPLOAD_DIR"], "receipts", os.path.basename(lot["receipt"])))


@app.route("/lot/<int:lot_id>/delete", methods=["POST"])
def lot_delete(lot_id):
    lot = q1("SELECT * FROM stock_lots WHERE id=?", lot_id) or abort(404)
    if q1("SELECT 1 FROM line_units WHERE lot_id=?", lot_id):
        flash("This stock is on an invoice, so it can't be deleted. Void the invoice first.", "error")
        return redirect(url_for("lot_view", lot_id=lot_id))
    x("DELETE FROM stock_lots WHERE id=?", lot_id)
    get_db().commit()
    flash("Stock entry deleted.", "ok")
    return redirect(url_for("product_view", product_id=lot["product_id"]))


# --------------------------------------------------------------------------- customers
@app.route("/customers")
def customers():
    text = request.args.get("q", "").strip()
    sql = """SELECT c.*, (SELECT COUNT(*) FROM documents WHERE customer_id=c.id) AS docs FROM customers c"""
    args = []
    if text:
        sql += " WHERE c.name LIKE ? OR c.email LIKE ?"
        args = [f"%{text}%"] * 2
    return render_template("customers.html", rows=q(sql + " ORDER BY c.name COLLATE NOCASE", *args), text=text)


@app.route("/customers/new", methods=["GET", "POST"])
@app.route("/customers/<int:cid>/edit", methods=["GET", "POST"])
def customer_edit(cid=None):
    cust = (q1("SELECT * FROM customers WHERE id=?", cid) or abort(404)) if cid else None
    f = dict(cust) if cust else dict(name="", address="", email="", phone="", notes="")
    if request.method == "POST":
        f = dict(request.form)
        name = request.form.get("name", "").strip()
        if not name:
            flash("Customer name is required.", "error")
        else:
            vals = (name, request.form.get("address", "").strip(), request.form.get("email", "").strip(),
                    request.form.get("phone", "").strip(), request.form.get("notes", "").strip())
            if cust:
                x("UPDATE customers SET name=?, address=?, email=?, phone=?, notes=? WHERE id=?", *vals, cid)
                new_id = cid
            else:
                new_id = x("INSERT INTO customers(name,address,email,phone,notes) VALUES(?,?,?,?,?)", *vals).lastrowid
            get_db().commit()
            flash("Customer saved.", "ok")
            nxt = request.args.get("doc")
            if nxt and nxt.isdigit():
                x("UPDATE documents SET customer_id=? WHERE id=? AND customer_id IS NULL", new_id, int(nxt))
                get_db().commit()
                return redirect(url_for("doc_view", doc_id=int(nxt)))
            return redirect(url_for("customers"))
    docs = []
    if cust:
        docs = [dict(d, pay=pay_status(d)) for d in q(DOC_SQL + " WHERE d.customer_id=? ORDER BY d.id DESC", cid)]
    return render_template("customer_form.html", f=f, cust=cust, docs=docs)


@app.route("/customers/<int:cid>/delete", methods=["POST"])
def customer_delete(cid):
    if q1("SELECT 1 FROM documents WHERE customer_id=?", cid):
        flash("This customer has quotes or invoices, so they can't be deleted.", "error")
        return redirect(url_for("customer_edit", cid=cid))
    x("DELETE FROM customers WHERE id=?", cid)
    get_db().commit()
    flash("Customer deleted.", "ok")
    return redirect(url_for("customers"))


# --------------------------------------------------------------------------- quotes + invoices
def _doc_list(kind, template):
    status = request.args.get("status", "")
    text = request.args.get("q", "").strip()
    sql = DOC_SQL + " WHERE d.kind=?"
    args = [kind]
    if text:
        sql += " AND (d.number LIKE ? OR c.name LIKE ?)"
        args += [f"%{text}%"] * 2
    rows = [dict(d, pay=pay_status(d)) for d in q(sql + " ORDER BY d.id DESC", *args)]
    if status:
        rows = [r for r in rows if r["pay"].lower().replace(" ", "") == status]
    return render_template(template, rows=rows, status=status, text=text, kind=kind,
                           outstanding=sum(r["total"] - r["paid"] for r in rows if r["pay"] in ("Unpaid", "Overdue", "Part paid")))


@app.route("/invoices")
def invoices():
    return _doc_list("invoice", "doc_list.html")


@app.route("/quotes")
def quotes():
    return _doc_list("quote", "doc_list.html")


@app.route("/new/<kind>", methods=["POST"])
def doc_new(kind):
    if kind not in ("invoice", "quote"):
        abort(404)
    cust = request.form.get("customer_id")
    cust = int(cust) if cust and cust.isdigit() else None
    t = today()
    number = None
    status = "draft"
    due = None
    if kind == "quote":
        number, status = next_number("quote"), "open"
        due = (t + dt.timedelta(days=int(get_settings()["quote_valid_days"]))).isoformat()
    cur = x("INSERT INTO documents(kind, number, customer_id, issue_date, due_date, status) VALUES(?,?,?,?,?,?)",
            kind, number, cust, t.isoformat(), due, status)
    get_db().commit()
    return redirect(url_for("doc_view", doc_id=cur.lastrowid))


@app.route("/doc/<int:doc_id>")
def doc_view(doc_id):
    d = get_doc(doc_id)
    lines = doc_lines(doc_id)
    customer = q1("SELECT * FROM customers WHERE id=?", d["customer_id"]) if d["customer_id"] else None
    prods = q("""SELECT p.*, COALESCE((SELECT SUM(qty_remaining) FROM stock_lots WHERE product_id=p.id),0) AS in_stock
                 FROM products p WHERE p.active=1 ORDER BY p.category, p.name""")
    payments = q("SELECT * FROM payments WHERE document_id=? ORDER BY paid_date, id", doc_id)
    cost = sum(l["cost"] for l in lines)
    source = q1("SELECT id, number FROM documents WHERE id=?", d["source_quote_id"]) if d["source_quote_id"] else None
    converted = q1("SELECT id, number FROM documents WHERE source_quote_id=? ORDER BY id DESC", doc_id) \
        if d["kind"] == "quote" else None
    return render_template("doc_view.html", d=d, lines=lines, customer=customer, prods=prods, payments=payments,
                           customers=q("SELECT id, name FROM customers ORDER BY name COLLATE NOCASE"),
                           editable=is_editable(d), pay=pay_status(d), cost=cost, source=source, converted=converted)


@app.route("/doc/<int:doc_id>/header", methods=["POST"])
def doc_header(doc_id):
    d = get_doc(doc_id)
    if not is_editable(d):
        abort(400)
    try:
        cust = request.form.get("customer_id")
        cust = int(cust) if cust and cust.isdigit() else None
        issue = parse_date(request.form.get("issue_date"), today()).isoformat()
        due = parse_date(request.form.get("due_date"))
        x("UPDATE documents SET customer_id=?, issue_date=?, due_date=?, notes=? WHERE id=?", cust, issue,
          due.isoformat() if due else None, request.form.get("notes", "").strip(), doc_id)
        get_db().commit()
        if request.form.get("then") == "issue" and d["kind"] == "invoice":
            return doc_issue(doc_id)  # details are saved first, then the invoice is issued
        flash("Saved.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("doc_view", doc_id=doc_id))


@app.route("/doc/<int:doc_id>/lines", methods=["POST"])
def line_add(doc_id):
    d = get_doc(doc_id)
    if not is_editable(d):
        abort(400)
    db = get_db()
    try:
        pid = request.form.get("product_id")
        product = q1("SELECT * FROM products WHERE id=?", pid) if pid and pid.isdigit() else None
        desc = request.form.get("description", "").strip() or (product["name"] if product else "")
        if not desc:
            raise ValueError("Pick a product or type a description.")
        qty = form_int("qty", default=1, min_value=1)
        price_raw = request.form.get("unit_price", "").strip()
        price = to_pence(price_raw, allow_negative=True) if price_raw else (product["sale_price"] if product else 0)
        pos = q1("SELECT COALESCE(MAX(position),0)+1 AS n FROM document_lines WHERE document_id=?", doc_id)["n"]
        line_id = x("INSERT INTO document_lines(document_id, position, product_id, description, qty, unit_price) "
                    "VALUES(?,?,?,?,?,?)", doc_id, pos, product["id"] if product else None, desc, qty, price).lastrowid
        if d["kind"] == "invoice" and product and not product["tracks_serials"]:
            allocate_bulk(line_id, product["id"], qty)
        db.commit()
        if d["kind"] == "invoice" and product and product["tracks_serials"]:
            flash("Now tick the serial numbers being sold on this line.", "ok")
            return redirect(url_for("line_units", line_id=line_id))
    except ValueError as e:
        db.rollback()
        flash(str(e), "error")
    return redirect(url_for("doc_view", doc_id=doc_id))


def _line_and_doc(line_id):
    ln = q1("""SELECT dl.*, p.tracks_serials FROM document_lines dl LEFT JOIN products p ON p.id=dl.product_id
               WHERE dl.id=?""", line_id) or abort(404)
    d = get_doc(ln["document_id"])
    if not is_editable(d):
        abort(400)
    return ln, d


@app.route("/line/<int:line_id>/update", methods=["POST"])
def line_update(line_id):
    ln, d = _line_and_doc(line_id)
    db = get_db()
    try:
        desc = request.form.get("description", "").strip()
        if not desc:
            raise ValueError("Description can't be empty.")
        price = to_pence(request.form.get("unit_price"), allow_negative=True)
        qty = ln["qty"]
        serial_invoice = d["kind"] == "invoice" and ln["product_id"] and ln["tracks_serials"]
        if not serial_invoice:
            qty = form_int("qty", default=ln["qty"], min_value=1)
        x("UPDATE document_lines SET description=?, unit_price=?, qty=? WHERE id=?", desc, price, qty, line_id)
        if d["kind"] == "invoice" and ln["product_id"] and not ln["tracks_serials"] and qty != ln["qty"]:
            release_line(line_id)
            allocate_bulk(line_id, ln["product_id"], qty)
        db.commit()
    except ValueError as e:
        db.rollback()
        flash(str(e), "error")
    return redirect(url_for("doc_view", doc_id=d["id"]))


@app.route("/line/<int:line_id>/delete", methods=["POST"])
def line_delete(line_id):
    _ln, d = _line_and_doc(line_id)
    release_line(line_id)
    x("DELETE FROM document_lines WHERE id=?", line_id)
    get_db().commit()
    return redirect(url_for("doc_view", doc_id=d["id"]))


@app.route("/line/<int:line_id>/units", methods=["GET", "POST"])
def line_units(line_id):
    ln, d = _line_and_doc(line_id)
    if d["kind"] != "invoice" or not ln["product_id"] or not ln["tracks_serials"]:
        abort(400)
    if request.method == "POST":
        db = get_db()
        try:
            chosen = [int(v) for v in request.form.getlist("lot_ids") if v.isdigit()]
            if not chosen:
                raise ValueError("Tick at least one serial number (or delete the line).")
            release_line(line_id)
            for lot_id in chosen:
                lot = q1("SELECT * FROM stock_lots WHERE id=? AND product_id=?", lot_id, ln["product_id"])
                if lot is None or lot["serial"] is None or lot["qty_remaining"] < 1:
                    raise ValueError("One of the ticked units is no longer available.")
                x("UPDATE stock_lots SET qty_remaining=0 WHERE id=?", lot_id)
                x("INSERT INTO line_units(line_id, lot_id, qty) VALUES(?,?,1)", line_id, lot_id)
            x("UPDATE document_lines SET qty=? WHERE id=?", len(chosen), line_id)
            db.commit()
            return redirect(url_for("doc_view", doc_id=d["id"]))
        except ValueError as e:
            db.rollback()
            flash(str(e), "error")
    units = q("""SELECT l.*, EXISTS(SELECT 1 FROM line_units lu WHERE lu.line_id=? AND lu.lot_id=l.id) AS selected
                 FROM stock_lots l WHERE l.product_id=? AND l.serial IS NOT NULL
                 AND (l.qty_remaining>0 OR EXISTS(SELECT 1 FROM line_units lu WHERE lu.line_id=? AND lu.lot_id=l.id))
                 ORDER BY l.purchase_date, l.serial""", line_id, ln["product_id"], line_id)
    return render_template("line_units.html", ln=ln, d=d, units=units)


@app.route("/doc/<int:doc_id>/issue", methods=["POST"])
def doc_issue(doc_id):
    d = get_doc(doc_id)
    if d["kind"] != "invoice" or d["status"] != "draft":
        abort(400)
    lines = doc_lines(doc_id)
    problems = []
    if not d["customer_id"]:
        problems.append("choose a customer")
    if not lines:
        problems.append("add at least one line")
    for ln in lines:
        if ln["needs_units"]:
            problems.append(f"choose serial numbers for '{ln['description']}'")
    if problems:
        flash("Before issuing: " + "; ".join(problems) + ".", "error")
        return redirect(url_for("doc_view", doc_id=doc_id))
    issue = d["issue_date"] or today().isoformat()
    due = d["due_date"] or (dt.date.fromisoformat(issue) + dt.timedelta(
        days=int(get_settings()["payment_terms_days"]))).isoformat()
    x("UPDATE documents SET number=?, status='issued', issue_date=?, due_date=? WHERE id=?",
      next_number("invoice"), issue, due, doc_id)
    get_db().commit()
    flash("Invoice issued. Stock is now marked as sold.", "ok")
    return redirect(url_for("doc_view", doc_id=doc_id))


@app.route("/doc/<int:doc_id>/void", methods=["POST"])
def doc_void(doc_id):
    d = get_doc(doc_id)
    if d["kind"] != "invoice" or d["status"] != "issued":
        abort(400)
    if d["paid"] > 0:
        flash("Delete the payments on this invoice first, then you can void it.", "error")
        return redirect(url_for("doc_view", doc_id=doc_id))
    release_document(doc_id)
    x("UPDATE documents SET status='void' WHERE id=?", doc_id)
    get_db().commit()
    flash("Invoice voided and its stock returned.", "ok")
    return redirect(url_for("doc_view", doc_id=doc_id))


@app.route("/doc/<int:doc_id>/delete", methods=["POST"])
def doc_delete(doc_id):
    d = get_doc(doc_id)
    if not is_editable(d) and not (d["kind"] == "quote"):
        flash("Only drafts can be deleted - void an issued invoice instead.", "error")
        return redirect(url_for("doc_view", doc_id=doc_id))
    release_document(doc_id)
    x("DELETE FROM document_lines WHERE document_id=?", doc_id)
    x("DELETE FROM documents WHERE id=?", doc_id)
    get_db().commit()
    flash("Deleted.", "ok")
    return redirect(url_for("invoices" if d["kind"] == "invoice" else "quotes"))


@app.route("/doc/<int:doc_id>/payments", methods=["POST"])
def payment_add(doc_id):
    d = get_doc(doc_id)
    if d["kind"] != "invoice" or d["status"] != "issued":
        abort(400)
    try:
        amount = to_pence(request.form.get("amount"))
        if amount <= 0:
            raise ValueError("Enter the amount received.")
        if amount > d["total"] - d["paid"]:
            raise ValueError(f"That's more than the balance due ({money(d['total'] - d['paid'])}).")
        paid = parse_date(request.form.get("paid_date"), today()).isoformat()
        x("INSERT INTO payments(document_id, paid_date, amount, method, reference) VALUES(?,?,?,?,?)",
          doc_id, paid, amount, request.form.get("method", ""), request.form.get("reference", "").strip())
        get_db().commit()
        flash("Payment recorded.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("doc_view", doc_id=doc_id))


@app.route("/payments/<int:pid>/delete", methods=["POST"])
def payment_delete(pid):
    p = q1("SELECT * FROM payments WHERE id=?", pid) or abort(404)
    x("DELETE FROM payments WHERE id=?", pid)
    get_db().commit()
    return redirect(url_for("doc_view", doc_id=p["document_id"]))


@app.route("/doc/<int:doc_id>/pdf")
def doc_pdf(doc_id):
    d = get_doc(doc_id)
    customer = q1("SELECT * FROM customers WHERE id=?", d["customer_id"]) if d["customer_id"] else None
    s = get_settings()
    logo = os.path.join(app.config["UPLOAD_DIR"], s["logo_file"]) if s.get("logo_file") else None
    lines = doc_lines(doc_id)
    data = build_pdf(d, customer, lines, s, logo, paid=d["paid"], total=d["total"])
    name = f"{'Invoice' if d['kind'] == 'invoice' else 'Quote'}-{d['number'] or 'DRAFT'}.pdf"
    return Response(data, mimetype="application/pdf", headers={
        "Content-Disposition": f'{"attachment" if request.args.get("download") else "inline"}; filename="{name}"'})


@app.route("/doc/<int:doc_id>/convert", methods=["POST"])
def quote_convert(doc_id):
    qd = get_doc(doc_id)
    if qd["kind"] != "quote":
        abort(400)
    db = get_db()
    try:
        t = today()
        due = (t + dt.timedelta(days=int(get_settings()["payment_terms_days"]))).isoformat()
        new_id = x("""INSERT INTO documents(kind, customer_id, issue_date, due_date, status, notes, source_quote_id)
                      VALUES('invoice',?,?,?,'draft',?,?)""", qd["customer_id"], t.isoformat(), due, qd["notes"],
                   doc_id).lastrowid
        for ln in q("""SELECT dl.*, p.tracks_serials FROM document_lines dl LEFT JOIN products p ON p.id=dl.product_id
                       WHERE dl.document_id=? ORDER BY dl.position, dl.id""", doc_id):
            new_line = x("""INSERT INTO document_lines(document_id, position, product_id, description, qty, unit_price)
                            VALUES(?,?,?,?,?,?)""", new_id, ln["position"], ln["product_id"], ln["description"],
                         ln["qty"], ln["unit_price"]).lastrowid
            if ln["product_id"] and ln["tracks_serials"] == 0:
                allocate_bulk(new_line, ln["product_id"], ln["qty"])
        x("UPDATE documents SET status='accepted' WHERE id=? AND status='open'", doc_id)
        db.commit()
        flash("Draft invoice created from the quote. Choose the serial numbers for any serialised items, then issue it.", "ok")
        return redirect(url_for("doc_view", doc_id=new_id))
    except ValueError as e:
        db.rollback()
        flash(f"Couldn't convert: {e}", "error")
        return redirect(url_for("doc_view", doc_id=doc_id))


@app.route("/doc/<int:doc_id>/quote-status", methods=["POST"])
def quote_status(doc_id):
    d = get_doc(doc_id)
    new = request.form.get("status")
    if d["kind"] != "quote" or new not in ("open", "accepted", "declined"):
        abort(400)
    x("UPDATE documents SET status=? WHERE id=?", new, doc_id)
    get_db().commit()
    return redirect(url_for("doc_view", doc_id=doc_id))


# --------------------------------------------------------------------------- expenses + other income
def _save_receipt(file_storage):
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic"):
        raise ValueError("Receipts must be a PDF or an image (png, jpg, webp, heic).")
    name = uuid.uuid4().hex + ext
    file_storage.save(os.path.join(app.config["UPLOAD_DIR"], "receipts", name))
    return name


def _drop_receipt(name):
    if name:
        try:
            os.remove(os.path.join(app.config["UPLOAD_DIR"], "receipts", os.path.basename(name)))
        except OSError:
            pass


@app.route("/expenses", methods=["GET", "POST"])
def expenses():
    if request.method == "POST":
        try:
            amount = to_pence(request.form.get("amount"))
            desc = request.form.get("description", "").strip()
            cat = request.form.get("category")
            if cat not in CAT_LABEL or not desc or amount <= 0:
                raise ValueError("Please fill in the date, category, description and a positive amount.")
            receipt = _save_receipt(request.files.get("receipt"))
            x("""INSERT INTO expenses(expense_date, category, description, supplier, amount, method, receipt)
                 VALUES(?,?,?,?,?,?,?)""", parse_date(request.form.get("expense_date"), today()).isoformat(), cat, desc,
              request.form.get("supplier", "").strip(), amount, request.form.get("method", ""), receipt)
            get_db().commit()
            flash("Expense added.", "ok")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("expenses", ty=pick_ty(request.args.get("ty"))))
    years = tax_years_available()
    ty = pick_ty(request.args.get("ty"))
    sql, args = "SELECT * FROM expenses", []
    if ty != "all":
        start, end = tax_year_range(int(ty))
        sql += " WHERE expense_date BETWEEN ? AND ?"
        args = [start.isoformat(), end.isoformat()]
    rows = q(sql + " ORDER BY expense_date DESC, id DESC", *args)
    return render_template("expenses.html", rows=rows, years=years, ty=ty, total=sum(r["amount"] for r in rows))


@app.route("/expenses/<int:eid>", methods=["GET", "POST"])
def expense_edit(eid):
    e = q1("SELECT * FROM expenses WHERE id=?", eid) or abort(404)
    if request.method == "POST":
        try:
            amount = to_pence(request.form.get("amount"))
            desc = request.form.get("description", "").strip()
            cat = request.form.get("category")
            if cat not in CAT_LABEL or not desc or amount <= 0:
                raise ValueError("Please fill in the category, description and a positive amount.")
            receipt = e["receipt"]
            new_receipt = _save_receipt(request.files.get("receipt"))
            if new_receipt:
                _drop_receipt(receipt)
                receipt = new_receipt
            elif request.form.get("remove_receipt"):
                _drop_receipt(receipt)
                receipt = None
            x("""UPDATE expenses SET expense_date=?, category=?, description=?, supplier=?, amount=?, method=?, receipt=?
                 WHERE id=?""", parse_date(request.form.get("expense_date"), today()).isoformat(), cat, desc,
              request.form.get("supplier", "").strip(), amount, request.form.get("method", ""), receipt, eid)
            get_db().commit()
            flash("Saved.", "ok")
            return redirect(url_for("expenses"))
        except ValueError as err:
            flash(str(err), "error")
    return render_template("expense_edit.html", e=e)


@app.route("/expenses/<int:eid>/delete", methods=["POST"])
def expense_delete(eid):
    e = q1("SELECT * FROM expenses WHERE id=?", eid) or abort(404)
    _drop_receipt(e["receipt"])
    x("DELETE FROM expenses WHERE id=?", eid)
    get_db().commit()
    flash("Expense deleted.", "ok")
    return redirect(url_for("expenses"))


@app.route("/receipt/<int:eid>")
def receipt(eid):
    e = q1("SELECT receipt FROM expenses WHERE id=?", eid)
    if not e or not e["receipt"]:
        abort(404)
    return send_file(os.path.join(app.config["UPLOAD_DIR"], "receipts", os.path.basename(e["receipt"])))


@app.route("/income", methods=["GET", "POST"])
def income():
    if request.method == "POST":
        try:
            amount = to_pence(request.form.get("amount"))
            desc = request.form.get("description", "").strip()
            if not desc or amount <= 0:
                raise ValueError("Enter a description and a positive amount.")
            x("INSERT INTO other_income(income_date, description, amount) VALUES(?,?,?)",
              parse_date(request.form.get("income_date"), today()).isoformat(), desc, amount)
            get_db().commit()
            flash("Income added.", "ok")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("income"))
    years = tax_years_available()
    ty = pick_ty(request.args.get("ty"))
    sql, args = "SELECT * FROM other_income", []
    if ty != "all":
        start, end = tax_year_range(int(ty))
        sql += " WHERE income_date BETWEEN ? AND ?"
        args = [start.isoformat(), end.isoformat()]
    rows = q(sql + " ORDER BY income_date DESC, id DESC", *args)
    return render_template("income.html", rows=rows, years=years, ty=ty, total=sum(r["amount"] for r in rows))


@app.route("/income/<int:iid>/delete", methods=["POST"])
def income_delete(iid):
    x("DELETE FROM other_income WHERE id=?", iid)
    get_db().commit()
    return redirect(url_for("income"))


# --------------------------------------------------------------------------- reports
@app.route("/reports")
def reports_sales():
    period = request.args.get("period", "month")
    if period not in ("week", "month", "year", "taxyear"):
        period = "month"
    rows = sales_table(period)
    totals = dict(count=sum(r["count"] for r in rows), invoiced=sum(r["invoiced"] for r in rows),
                  cost=sum(r["cost"] for r in rows), received=sum(r["received"] for r in rows),
                  other=sum(r["other"] for r in rows))
    totals["profit"] = totals["invoiced"] - totals["cost"]
    totals["margin"] = pct(totals["profit"], totals["invoiced"])
    if request.args.get("csv"):
        return csv_response(f"sales-by-{period}.csv",
                            ["Period", "Invoices", "Invoiced", "Cost of goods", "Gross profit", "Margin %",
                             "Cash received", "Other income"],
                            [[r["label"], r["count"], gbp(r["invoiced"]), gbp(r["cost"]), gbp(r["profit"]),
                              r["margin"] if r["margin"] is not None else "", gbp(r["received"]), gbp(r["other"])]
                             for r in rows])
    return render_template("reports_sales.html", rows=rows, period=period, totals=totals)


@app.route("/reports/tax")
def reports_tax():
    years = tax_years_available()
    try:
        year = int(request.args.get("ty", years[0]))
    except ValueError:
        year = years[0]
    summary = tax_summary(year)
    if request.args.get("csv"):
        rows = [["Turnover - invoices paid in the year (cash basis)", gbp(summary["received"])],
                ["Other income", gbp(summary["other"])], ["TOTAL TURNOVER", gbp(summary["turnover"])], []]
        for heading, h in summary["headings"]:
            rows.append([heading.upper(), gbp(h["total"])])
            rows += [["   " + label, gbp(amount)] for label, amount in h["items"]]
        rows += [[], ["TOTAL ALLOWABLE EXPENSES", gbp(summary["expenses_total"])], ["NET PROFIT", gbp(summary["net"])]]
        return csv_response(f"tax-summary-{summary['label'].replace('/', '-')}.csv", ["Item", "Amount (GBP)"], rows)
    return render_template("reports_tax.html", s=summary, years=years)


@app.route("/export/<what>.csv")
def export_csv(what):
    start_year = request.args.get("ty")
    a, b = "0000-01-01", "9999-12-31"
    if start_year and start_year.isdigit():
        s, e = tax_year_range(int(start_year))
        a, b = s.isoformat(), e.isoformat()
    if what == "expenses":
        rows = q("SELECT * FROM expenses WHERE expense_date BETWEEN ? AND ? ORDER BY expense_date", a, b)
        return csv_response("expenses.csv", ["Date", "Category", "HMRC heading", "Description", "Supplier", "Amount",
                                             "Paid by"],
                            [[r["expense_date"], CAT_LABEL.get(r["category"], r["category"]),
                              CAT_HEADING.get(r["category"], ""), r["description"], r["supplier"], gbp(r["amount"]),
                              r["method"]] for r in rows])
    if what == "income":
        rows = q("""SELECT p.paid_date, d.number, c.name, p.amount, p.method, p.reference FROM payments p
                    JOIN documents d ON d.id=p.document_id LEFT JOIN customers c ON c.id=d.customer_id
                    WHERE d.status='issued' AND p.paid_date BETWEEN ? AND ? ORDER BY p.paid_date""", a, b)
        other = q("SELECT * FROM other_income WHERE income_date BETWEEN ? AND ? ORDER BY income_date", a, b)
        data = [[r["paid_date"], r["number"], r["name"], gbp(r["amount"]), r["method"], r["reference"]] for r in rows]
        data += [[r["income_date"], "", "", gbp(r["amount"]), "", r["description"]] for r in other]
        return csv_response("income.csv", ["Date received", "Invoice", "Customer", "Amount", "Method", "Reference"],
                            sorted(data, key=lambda r: r[0]))
    if what == "invoices":
        rows = q(DOC_SQL + " WHERE d.kind='invoice' AND d.status IN ('issued','void') ORDER BY d.issue_date, d.id")
        return csv_response("invoices.csv", ["Number", "Date", "Customer", "Status", "Total", "Paid", "Balance"],
                            [[r["number"], r["issue_date"], r["customer_name"], pay_status(r), gbp(r["total"]),
                              gbp(r["paid"]), gbp(r["total"] - r["paid"])] for r in rows])
    if what == "stock":
        rows = q("""SELECT p.name, p.category, l.serial, l.qty_initial, l.qty_remaining, l.unit_cost,
                    COALESCE(l.list_price,p.sale_price) AS price, l.purchase_date, l.supplier, l.condition,
                    (SELECT d.number FROM line_units lu JOIN document_lines dl ON dl.id=lu.line_id
                     JOIN documents d ON d.id=dl.document_id WHERE lu.lot_id=l.id LIMIT 1) AS invoice
                    FROM stock_lots l JOIN products p ON p.id=l.product_id ORDER BY p.name, l.serial""")
        return csv_response("stock.csv", ["Product", "Category", "Serial", "Qty bought", "Qty in stock", "Unit cost",
                                          "Sale price", "Bought", "Supplier", "Condition", "Invoice"],
                            [[r["name"], r["category"], r["serial"], r["qty_initial"], r["qty_remaining"],
                              gbp(r["unit_cost"]), gbp(r["price"]), r["purchase_date"], r["supplier"], r["condition"],
                              r["invoice"]] for r in rows])
    abort(404)


# --------------------------------------------------------------------------- settings, users, backups
@app.route("/settings")
def settings():
    return render_template("settings.html", users=q("SELECT id, username FROM users ORDER BY username"),
                           backups=list_backups(app.config["BACKUP_DIR"]), backup_hour=app.config["BACKUP_HOUR"],
                           backup_keep=app.config["BACKUP_KEEP"])


@app.route("/settings/business", methods=["POST"])
def settings_business():
    try:
        for key in ("business_name", "business_address", "business_email", "business_phone", "bank_details",
                    "invoice_prefix", "quote_prefix", "invoice_footer", "quote_footer"):
            set_setting(key, request.form.get(key, "").strip())
        for key in ("next_invoice_no", "next_quote_no", "payment_terms_days", "quote_valid_days"):
            val = form_int(key, min_value=1 if "next" in key else 0)
            set_setting(key, str(val))
        method = request.form.get("stock_method", "purchases")
        set_setting("stock_method", method if method in ("purchases", "cogs") else "purchases")
        get_db().commit()
        flash("Settings saved.", "ok")
    except ValueError as e:
        get_db().rollback()
        flash(str(e), "error")
    return redirect(url_for("settings"))


@app.route("/settings/logo", methods=["POST"])
def settings_logo():
    s = get_settings()
    if request.form.get("remove"):
        old = s.get("logo_file")
        if old:
            try:
                os.remove(os.path.join(app.config["UPLOAD_DIR"], old))
            except OSError:
                pass
        set_setting("logo_file", "")
        get_db().commit()
        flash("Logo removed.", "ok")
        return redirect(url_for("settings"))
    f = request.files.get("logo")
    if not f or not f.filename:
        flash("Choose an image file first.", "error")
        return redirect(url_for("settings"))
    try:
        img = Image.open(f.stream)
        img.load()
    except Exception:
        flash("That file isn't a picture I can read. Use PNG or JPG.", "error")
        return redirect(url_for("settings"))
    img.thumbnail((1200, 1200))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    name = f"logo-{uuid.uuid4().hex[:8]}.png"
    img.save(os.path.join(app.config["UPLOAD_DIR"], name), "PNG")
    old = s.get("logo_file")
    if old:
        try:
            os.remove(os.path.join(app.config["UPLOAD_DIR"], old))
        except OSError:
            pass
    set_setting("logo_file", name)
    get_db().commit()
    flash("Logo updated.", "ok")
    return redirect(url_for("settings"))


@app.route("/logo")
def logo():
    name = get_settings().get("logo_file")
    if not name:
        abort(404)
    return send_file(os.path.join(app.config["UPLOAD_DIR"], os.path.basename(name)), mimetype="image/png")


@app.route("/settings/password", methods=["POST"])
def settings_password():
    row = q1("SELECT * FROM users WHERE id=?", g.user["id"])
    new = request.form.get("new_password", "")
    if not check_password_hash(row["password_hash"], request.form.get("current_password", "")):
        flash("Your current password is wrong.", "error")
    elif len(new) < 8 or new != request.form.get("new_password2"):
        flash("New password must be at least 8 characters and typed twice the same.", "error")
    else:
        x("UPDATE users SET password_hash=? WHERE id=?", generate_password_hash(new), g.user["id"])
        get_db().commit()
        flash("Password changed.", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/users", methods=["POST"])
def user_add():
    name = request.form.get("username", "").strip()
    pw = request.form.get("password", "")
    if not name or len(pw) < 8:
        flash("Username required and password must be at least 8 characters.", "error")
    elif q1("SELECT 1 FROM users WHERE username=?", name):
        flash("That username is taken.", "error")
    else:
        x("INSERT INTO users(username, password_hash) VALUES(?,?)", name, generate_password_hash(pw))
        get_db().commit()
        flash("User added.", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/users/<int:uid>/delete", methods=["POST"])
def user_delete(uid):
    if uid == g.user["id"]:
        flash("You can't delete the account you're signed in with.", "error")
    else:
        x("DELETE FROM users WHERE id=?", uid)
        get_db().commit()
        flash("User removed.", "ok")
    return redirect(url_for("settings"))


@app.route("/backup/now", methods=["POST"])
def backup_now():
    name = make_backup(app.config["DB_PATH"], app.config["UPLOAD_DIR"], app.config["BACKUP_DIR"],
                       app.config["BACKUP_KEEP"])
    flash(f"Backup written: {name}", "ok")
    return redirect(url_for("settings"))


@app.route("/backup/<name>")
def backup_download(name):
    if not NAME_RE.match(name):
        abort(404)
    path = os.path.join(app.config["BACKUP_DIR"], name)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True)
