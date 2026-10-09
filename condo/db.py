"""ฐานข้อมูล SQLite: การเชื่อมต่อ, สร้างตาราง และข้อมูลเริ่มต้น"""
import json
import os
import sqlite3

from flask import current_app, g
from werkzeug.security import generate_password_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS units (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_no     TEXT NOT NULL UNIQUE,
    building    TEXT DEFAULT '',
    floor       TEXT DEFAULT '',
    area_sqm    REAL NOT NULL DEFAULT 0,
    owner_name  TEXT DEFAULT '',
    phone       TEXT DEFAULT '',
    email       TEXT DEFAULT '',
    note        TEXT DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name     TEXT DEFAULT '',
    role          TEXT NOT NULL CHECK (role IN ('admin', 'resident')),
    unit_id       INTEGER REFERENCES units(id) ON DELETE SET NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    last_login    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ประเภทค่าใช้จ่าย/ค่าบริการ ที่แอดมินกำหนดวิธีคิดได้เอง
CREATE TABLE IF NOT EXISTS charge_types (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    description  TEXT DEFAULT '',
    method       TEXT NOT NULL CHECK (method IN ('meter_rate', 'meter_tiered', 'fixed', 'per_area')),
    rate         REAL NOT NULL DEFAULT 0,      -- บาท/หน่วย, บาท/เดือน หรือ บาท/ตร.ม.
    tiers        TEXT DEFAULT '[]',            -- JSON สำหรับคิดแบบขั้นบันได
    fixed_fee    REAL NOT NULL DEFAULT 0,      -- ค่าบริการรักษามิเตอร์ ฯลฯ บวกเพิ่ม
    min_charge   REAL NOT NULL DEFAULT 0,      -- ค่าขั้นต่ำ
    unit_label   TEXT DEFAULT 'หน่วย',
    vat_percent  REAL NOT NULL DEFAULT 0,
    frequency    TEXT NOT NULL DEFAULT 'monthly' CHECK (frequency IN ('monthly', 'yearly', 'once')),
    bill_month   INTEGER,                      -- เดือนที่เรียกเก็บ (รายปี)
    start_period TEXT,                         -- YYYY-MM เริ่มเรียกเก็บ
    end_period   TEXT,                         -- YYYY-MM สิ้นสุด
    apply_to     TEXT NOT NULL DEFAULT 'all' CHECK (apply_to IN ('all', 'selected')),
    sort_order   INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ห้องที่ใช้ค่าบริการ (กรณีเลือกเฉพาะห้อง) และยอดเฉพาะห้อง (ถ้ามี)
CREATE TABLE IF NOT EXISTS charge_type_units (
    charge_type_id  INTEGER NOT NULL REFERENCES charge_types(id) ON DELETE CASCADE,
    unit_id         INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    amount_override REAL,
    PRIMARY KEY (charge_type_id, unit_id)
);

CREATE TABLE IF NOT EXISTS meter_readings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    charge_type_id INTEGER NOT NULL REFERENCES charge_types(id) ON DELETE CASCADE,
    unit_id        INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    period         TEXT NOT NULL,
    prev_reading   REAL NOT NULL,
    curr_reading   REAL NOT NULL,
    recorded_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (charge_type_id, unit_id, period)
);

-- ค่าใช้จ่ายเฉพาะครั้ง/เฉพาะห้อง เช่น ค่าซ่อม ค่าบัตรจอดรถ
CREATE TABLE IF NOT EXISTS adhoc_charges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id     INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    period      TEXT NOT NULL,
    description TEXT NOT NULL,
    amount      REAL NOT NULL,
    invoice_id  INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS invoices (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_no        TEXT NOT NULL UNIQUE,
    unit_id           INTEGER NOT NULL REFERENCES units(id),
    unit_no           TEXT NOT NULL,          -- เก็บสำเนาไว้เป็นประวัติ
    owner_name        TEXT DEFAULT '',
    period            TEXT NOT NULL,
    issue_date        TEXT NOT NULL,
    due_date          TEXT NOT NULL,
    subtotal          REAL NOT NULL DEFAULT 0,
    vat               REAL NOT NULL DEFAULT 0,
    total             REAL NOT NULL DEFAULT 0,
    paid_amount       REAL NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'unpaid' CHECK (status IN ('unpaid', 'partial', 'paid', 'void')),
    note              TEXT DEFAULT '',
    created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_invoices_period ON invoices(period);
CREATE INDEX IF NOT EXISTS idx_invoices_unit ON invoices(unit_id);

CREATE TABLE IF NOT EXISTS invoice_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id     INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    charge_type_id INTEGER REFERENCES charge_types(id) ON DELETE SET NULL,
    description    TEXT NOT NULL,
    detail         TEXT DEFAULT '',
    quantity       REAL NOT NULL DEFAULT 1,
    unit_label     TEXT DEFAULT '',
    unit_price     REAL NOT NULL DEFAULT 0,
    amount         REAL NOT NULL DEFAULT 0,
    vat_amount     REAL NOT NULL DEFAULT 0,
    sort_order     INTEGER NOT NULL DEFAULT 0,
    kind           TEXT NOT NULL DEFAULT 'auto',  -- auto / manual / penalty
    meter_prev     REAL,
    meter_curr     REAL
);

CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id  INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    receipt_no  TEXT NOT NULL UNIQUE,
    paid_at     TEXT NOT NULL,
    amount      REAL NOT NULL,
    method      TEXT DEFAULT 'โอนเงิน',
    reference   TEXT DEFAULT '',
    note        TEXT DEFAULT '',
    created_by  INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS activity_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    username   TEXT,
    action     TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

DEFAULT_SETTINGS = {
    "condo_name": "นิติบุคคลอาคารชุด ตัวอย่างคอนโด",
    "address": "",
    "phone": "",
    "tax_id": "",
    "issue_day": "1",          # วันที่ออกใบแจ้งหนี้
    "due_days": "15",          # ครบกำหนดชำระภายในกี่วันหลังออกบิล
    "penalty_types": "เบี้ยปรับ\nเบี้ยปรับค่าน้ำ",  # ชนิดเบี้ยปรับที่แอดมินกรอกเอง บรรทัดละ 1 ชนิด
    "bank_info": "ธนาคาร ............ เลขที่บัญชี ............ ชื่อบัญชี ............",
    "invoice_note": "กรุณาชำระภายในวันครบกำหนด หากเกินกำหนดจะมีเบี้ยปรับตามระเบียบนิติบุคคล",
}

DEFAULT_CHARGE_TYPES = [
    dict(name="ค่าส่วนกลาง", method="fixed", rate=250, unit_label="เดือน", sort_order=5, active=1,
         description="เหมาจ่ายเดือนละ 250 บาทต่อห้อง"),
    dict(name="ค่าน้ำประปา", method="meter_rate", rate=18, min_charge=100, fixed_fee=0,
         unit_label="ลบ.ม.", sort_order=10, active=1,
         description="คิดตามมิเตอร์ หน่วยละ 18 บาท ขั้นต่ำ 100 บาท"),
    dict(name="ค่าไฟฟ้า", method="meter_rate", rate=8, min_charge=0, fixed_fee=0,
         unit_label="kWh", sort_order=20, active=1,
         description="คิดตามมิเตอร์ หน่วยละ 8 บาท"),
    dict(name="ค่าขยะ", method="fixed", rate=20, unit_label="เดือน", sort_order=30, active=1,
         description="ห้องละ 20 บาทต่อเดือน"),
    dict(name="ค่าประกัน", method="fixed", rate=10, unit_label="เดือน", sort_order=40, active=1,
         description="ห้องละ 10 บาทต่อเดือน"),
]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def migrate(db):
    """อัปเดตฐานข้อมูลเดิมให้มีคอลัมน์ใหม่"""
    cols = {r["name"] for r in db.execute("PRAGMA table_info(invoice_items)")}
    for name, ddl in (("kind", "TEXT NOT NULL DEFAULT 'auto'"), ("meter_prev", "REAL"), ("meter_curr", "REAL")):
        if name not in cols:
            db.execute(f"ALTER TABLE invoice_items ADD COLUMN {name} {ddl}")
    # รายการที่ไม่ได้มาจากค่าบริการ (รายการเพิ่มเติมเดิม) ถือเป็นรายการที่แอดมินใส่เอง
    if "kind" not in cols:
        db.execute("UPDATE invoice_items SET kind='manual' WHERE charge_type_id IS NULL")


def init_db(db):
    db.executescript(SCHEMA)
    migrate(db)
    for key, value in DEFAULT_SETTINGS.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        db.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?, ?, ?, 'admin')",
            ("admin", generate_password_hash(os.environ.get("CONDO_ADMIN_PASSWORD") or "admin123"), "ผู้ดูแลระบบ"),
        )
    if db.execute("SELECT COUNT(*) FROM charge_types").fetchone()[0] == 0:
        for ct in DEFAULT_CHARGE_TYPES:
            ct = {"fixed_fee": 0, "min_charge": 0, **ct, "tiers": json.dumps([])}
            cols = ", ".join(ct)
            db.execute(
                f"INSERT INTO charge_types ({cols}) VALUES ({', '.join('?' * len(ct))})",
                list(ct.values()),
            )
    db.commit()


def get_settings():
    rows = get_db().execute("SELECT key, value FROM settings").fetchall()
    data = dict(DEFAULT_SETTINGS)
    data.update({r["key"]: r["value"] for r in rows})
    return data


def log_activity(user, action):
    db = get_db()
    db.execute(
        "INSERT INTO activity_log (user_id, username, action) VALUES (?, ?, ?)",
        (user["id"] if user else None, user["username"] if user else None, action),
    )
