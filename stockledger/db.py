import sqlite3

from flask import current_app, g

# All money is stored as whole pence (integers) so totals never drift.
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS customers(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  address TEXT, email TEXT, phone TEXT, notes TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY,
  sku TEXT,
  name TEXT NOT NULL,
  category TEXT,
  tracks_serials INTEGER NOT NULL DEFAULT 1,
  sale_price INTEGER NOT NULL DEFAULT 0,
  barcode TEXT,
  notes TEXT,
  active INTEGER NOT NULL DEFAULT 1
);
-- One row per purchase "lot". Serial-tracked items are one row per unit (qty 1, serial set).
-- Non-serial items are one row per batch; qty_remaining counts down as they are sold.
-- qty_remaining is also reduced while an item sits on a draft invoice (it is reserved).
CREATE TABLE IF NOT EXISTS stock_lots(
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id),
  serial TEXT,
  qty_initial INTEGER NOT NULL,
  qty_remaining INTEGER NOT NULL,
  unit_cost INTEGER NOT NULL,
  list_price INTEGER,
  purchase_date TEXT NOT NULL,
  supplier TEXT,
  condition TEXT,
  notes TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS lot_serial ON stock_lots(product_id, serial) WHERE serial IS NOT NULL;
CREATE INDEX IF NOT EXISTS lot_product ON stock_lots(product_id);
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('invoice','quote')),
  number TEXT,
  customer_id INTEGER REFERENCES customers(id),
  issue_date TEXT,
  due_date TEXT,
  status TEXT NOT NULL,
  notes TEXT,
  source_quote_id INTEGER,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS doc_number ON documents(kind, number) WHERE number IS NOT NULL;
CREATE TABLE IF NOT EXISTS document_lines(
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  position INTEGER NOT NULL DEFAULT 0,
  product_id INTEGER REFERENCES products(id),
  description TEXT NOT NULL,
  qty INTEGER NOT NULL DEFAULT 1,
  unit_price INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS line_units(
  id INTEGER PRIMARY KEY,
  line_id INTEGER NOT NULL REFERENCES document_lines(id) ON DELETE CASCADE,
  lot_id INTEGER NOT NULL REFERENCES stock_lots(id),
  qty INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS lu_lot ON line_units(lot_id);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  paid_date TEXT NOT NULL,
  amount INTEGER NOT NULL,
  method TEXT,
  reference TEXT
);
CREATE TABLE IF NOT EXISTS expenses(
  id INTEGER PRIMARY KEY,
  expense_date TEXT NOT NULL,
  category TEXT NOT NULL,
  description TEXT NOT NULL,
  supplier TEXT,
  amount INTEGER NOT NULL,
  method TEXT,
  receipt TEXT,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS other_income(
  id INTEGER PRIMARY KEY,
  income_date TEXT NOT NULL,
  description TEXT NOT NULL,
  amount INTEGER NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "business_name": "My Business",
    "business_address": "",
    "business_email": "",
    "business_phone": "",
    "bank_details": "",
    "invoice_prefix": "INV-",
    "quote_prefix": "QUO-",
    "next_invoice_no": "1",
    "next_quote_no": "1",
    "payment_terms_days": "14",
    "quote_valid_days": "30",
    "invoice_footer": "Thank you for your business.",
    "quote_footer": "This quote is subject to stock availability.",
    "logo_file": "",
    # 'purchases' = deduct stock when bought (HMRC cash basis).  'cogs' = deduct cost of goods sold.
    "stock_method": "purchases",
}


def connect(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(path):
    conn = connect(path)
    conn.executescript(SCHEMA)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(stock_lots)")]
    if "receipt" not in cols:  # upgrade older databases
        conn.execute("ALTER TABLE stock_lots ADD COLUMN receipt TEXT")
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))
    conn.commit()
    conn.close()


def get_settings():
    if "settings" not in g:
        rows = get_db().execute("SELECT key, value FROM settings").fetchall()
        g.settings = {r["key"]: r["value"] for r in rows}
    return g.settings


def set_setting(key, value):
    get_db().execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    g.pop("settings", None)
