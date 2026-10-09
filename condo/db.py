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
    unit_type   TEXT NOT NULL DEFAULT 'room',  -- room = ห้องชุด, shop = ร้านค้าหน้าอาคาร
    building    TEXT DEFAULT '',
    floor       TEXT DEFAULT '',
    area_sqm    REAL NOT NULL DEFAULT 0,
    owner_name  TEXT DEFAULT '',              -- เจ้าของห้องชุด
    phone       TEXT DEFAULT '',              -- เบอร์โทรเจ้าของ
    tenant_name TEXT DEFAULT '',              -- ผู้เช่า / ผู้พักอาศัย (ถ้ามี)
    tenant_phone TEXT DEFAULT '',
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
    unit_type    TEXT NOT NULL DEFAULT 'all',  -- all / room / shop
    manual_amount INTEGER NOT NULL DEFAULT 0,  -- 1 = แอดมินกรอกยอดเองแต่ละห้อง (เช่น ค่าเช่าพื้นที่)
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
    read_date      TEXT,                       -- วันที่จดมิเตอร์
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
    kind        TEXT NOT NULL DEFAULT 'manual',  -- manual / penalty / other (ช่อง "อื่น ๆ" ในหน้ากรอกรวม)
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS invoices (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_no        TEXT NOT NULL UNIQUE,
    unit_id           INTEGER NOT NULL REFERENCES units(id),
    unit_no           TEXT NOT NULL,          -- เก็บสำเนาไว้เป็นประวัติ
    owner_name        TEXT DEFAULT '',
    tenant_name       TEXT DEFAULT '',
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
    meter_curr     REAL,
    meter_prev_date TEXT,
    meter_curr_date TEXT
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

-- ค่าบริการที่ไม่เรียกเก็บจากห้องนี้ในงวดนี้ (ติ๊กออกในหน้ากรอกรวม)
CREATE TABLE IF NOT EXISTS bill_exclusions (
    unit_id        INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    period         TEXT NOT NULL,
    charge_type_id INTEGER NOT NULL REFERENCES charge_types(id) ON DELETE CASCADE,
    PRIMARY KEY (unit_id, period, charge_type_id)
);

CREATE TABLE IF NOT EXISTS activity_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    username   TEXT,
    action     TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

EXAMPLE_CONDO_NAME = "นิติบุคคลอาคารชุด ตัวอย่างคอนโด"

DEFAULT_SETTINGS = {
    "condo_name": "นิติบุคคลอาคารชุดเคหะชุมชนคลองจั่น 26",
    "address": "ถ.นวมินทร์ แขวงคลองจั่น เขตบางกะปิ กรุงเทพมหานคร",
    "phone": "02-3755395",
    "tax_id": "",
    "house_code": "1006-196055-1",   # รหัสประจำบ้าน
    "manager_title": "ผู้จัดการนิติบุคคลอาคารชุดเคหะชุมชนคลองจั่น 26",
    "receipt_footer": "ใบเสร็จนี้จะสมบูรณ์ต่อเมื่อมีลายมือชื่อผู้จัดการนิติบุคคลอาคารชุด พนักงานเก็บเงิน"
                      " และประทับตราของนิติบุคคลอาคารชุดเคหะชุมชนคลองจั่น 26",
    "print_mode": "color",           # color = พิมพ์สี, eco = ประหยัดหมึก (ขาวดำ ไม่มีพื้นหลัง)
    "auto_print_receipt": "1",       # รับชำระแล้วพิมพ์ใบเสร็จทันที
    "logo": "",                      # รูปโลโก้ที่อัปโหลด (data URL) ว่าง = ใช้ static/logo.png
    "issue_day": "1",          # วันที่ออกใบแจ้งหนี้
    "due_days": "15",          # ครบกำหนดชำระภายในกี่วันหลังออกบิล
    "penalty_types": "เบี้ยปรับ\nเบี้ยปรับค่าน้ำ",  # ชนิดเบี้ยปรับที่แอดมินกรอกเอง บรรทัดละ 1 ชนิด
    "bank_info": "ธนาคาร ............ เลขที่บัญชี ............ ชื่อบัญชี ............",
    "invoice_note": "กรุณาชำระภายในวันครบกำหนด หากเกินกำหนดจะมีเบี้ยปรับตามระเบียบนิติบุคคล",
}

WATER_DESCRIPTION = "ใช้ 1-4 หน่วย เหมาจ่าย 65 บาท / 5 หน่วยขึ้นไป หน่วยละ 16 บาท"

# ค่าบริการของร้านค้าหน้าอาคาร
SHOP_CHARGE_TYPES = [
    dict(name="ค่าเช่าพื้นที่", method="fixed", rate=0, unit_label="เดือน", sort_order=1, active=1,
         unit_type="shop", manual_amount=1, description="กรอกค่าเช่าของแต่ละร้านเอง"),
    dict(name="ค่าน้ำประปา (ร้านค้า)", method="meter_rate", rate=18, unit_label="หน่วย", sort_order=10, active=1,
         unit_type="shop", description="หน่วยละ 18 บาท"),
    dict(name="ค่ารักษามิเตอร์", method="fixed", rate=25, unit_label="เดือน", sort_order=11, active=1,
         unit_type="all", description="ค่ารักษามิเตอร์น้ำ 25 บาทต่อเดือน (ห้องชุดและร้านค้า)"),
    dict(name="ค่าไฟฟ้า (ร้านค้า)", method="meter_rate", rate=8, unit_label="หน่วย", sort_order=20, active=1,
         unit_type="shop", description="หน่วยละ 8 บาท"),
]

DEFAULT_CHARGE_TYPES = [
    dict(name="ค่าส่วนกลาง", method="fixed", rate=250, unit_label="เดือน", sort_order=5, active=1,
         description="เหมาจ่ายเดือนละ 250 บาทต่อห้อง"),
    dict(name="ค่าน้ำประปา", method="meter_rate", rate=16, min_charge=65, fixed_fee=0,
         unit_label="หน่วย", sort_order=10, active=1, description=WATER_DESCRIPTION),
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


def add_columns(db, table, columns):
    """เพิ่มคอลัมน์ที่ยังไม่มี คืนชื่อคอลัมน์ที่เพิ่งเพิ่ม"""
    existing = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
    added = []
    for name, ddl in columns:
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            added.append(name)
    return added


def insert_charge_type(db, ct):
    ct = {"fixed_fee": 0, "min_charge": 0, **ct, "tiers": json.dumps([])}
    db.execute(f"INSERT INTO charge_types ({', '.join(ct)}) VALUES ({', '.join('?' * len(ct))})", list(ct.values()))


def migrate(db):
    """อัปเดตฐานข้อมูลเดิมให้มีคอลัมน์ใหม่"""
    add_columns(db, "units", [("unit_type", "TEXT NOT NULL DEFAULT 'room'")])
    add_columns(db, "adhoc_charges", [("kind", "TEXT NOT NULL DEFAULT 'manual'")])
    if "unit_type" in add_columns(db, "charge_types", [("unit_type", "TEXT NOT NULL DEFAULT 'all'"),
                                                       ("manual_amount", "INTEGER NOT NULL DEFAULT 0")]):
        # ค่าบริการเดิมทั้งหมดเป็นของห้องชุด ร้านค้ามีชุดค่าบริการของตัวเอง
        db.execute("UPDATE charge_types SET unit_type='room'")
    cols = {r["name"] for r in db.execute("PRAGMA table_info(invoice_items)")}
    for name, ddl in (("kind", "TEXT NOT NULL DEFAULT 'auto'"), ("meter_prev", "REAL"), ("meter_curr", "REAL"),
                      ("meter_prev_date", "TEXT"), ("meter_curr_date", "TEXT")):
        if name not in cols:
            db.execute(f"ALTER TABLE invoice_items ADD COLUMN {name} {ddl}")
    if "read_date" not in {r["name"] for r in db.execute("PRAGMA table_info(meter_readings)")}:
        db.execute("ALTER TABLE meter_readings ADD COLUMN read_date TEXT")
    unit_cols = {r["name"] for r in db.execute("PRAGMA table_info(units)")}
    for name in ("tenant_name", "tenant_phone"):
        if name not in unit_cols:
            db.execute(f"ALTER TABLE units ADD COLUMN {name} TEXT DEFAULT ''")
    if "tenant_name" not in {r["name"] for r in db.execute("PRAGMA table_info(invoices)")}:
        db.execute("ALTER TABLE invoices ADD COLUMN tenant_name TEXT DEFAULT ''")
    # อัตราค่าน้ำเดิม (18 บาท ขั้นต่ำ 100) ที่ยังไม่ได้แก้ -> อัตราจริงของนิติ
    db.execute("UPDATE charge_types SET rate=16, min_charge=65, unit_label='หน่วย', description=?"
               " WHERE name='ค่าน้ำประปา' AND method='meter_rate' AND rate=18 AND min_charge=100",
               (WATER_DESCRIPTION,))
    # ฐานข้อมูลที่ยังใช้ชื่อตัวอย่าง: เปลี่ยนเป็นข้อมูลนิติบุคคลจริง (เฉพาะช่องที่ยังไม่ได้แก้)
    row = db.execute("SELECT value FROM settings WHERE key='condo_name'").fetchone()
    if row and row[0] == EXAMPLE_CONDO_NAME:
        db.execute("UPDATE settings SET value=? WHERE key='condo_name'", (DEFAULT_SETTINGS["condo_name"],))
        for key in ("address", "phone"):
            db.execute("UPDATE settings SET value=? WHERE key=? AND COALESCE(value,'')=''",
                       (DEFAULT_SETTINGS[key], key))
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
    reset = os.environ.get("CONDO_RESET_ADMIN_PASSWORD")
    if reset:
        # ลืมรหัสผ่าน: ตั้งตัวแปรนี้แล้วรีสตาร์ท ระบบจะตั้งรหัส admin ใหม่ (ลบตัวแปรออกหลังเข้าได้แล้ว)
        if db.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
            db.execute("UPDATE users SET password_hash=?, role='admin', active=1 WHERE username='admin'",
                       (generate_password_hash(reset),))
        else:
            db.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES ('admin', ?, ?, 'admin')",
                       (generate_password_hash(reset), "ผู้ดูแลระบบ"))
    if db.execute("SELECT COUNT(*) FROM charge_types").fetchone()[0] == 0:
        for ct in DEFAULT_CHARGE_TYPES:
            insert_charge_type(db, {"unit_type": "room", **ct})
    # ห้องชุดจ่ายค่าไฟกับการไฟฟ้าโดยตรง: ปิดค่าไฟของห้องชุด (ครั้งเดียว แอดมินเปิดกลับได้)
    if not db.execute("SELECT 1 FROM settings WHERE key='_room_electric_off'").fetchone():
        db.execute("UPDATE charge_types SET active=0 WHERE name='ค่าไฟฟ้า' AND unit_type='room'")
        db.execute("INSERT INTO settings (key, value) VALUES ('_room_electric_off', '1')")
    # ค่ารักษามิเตอร์ 25 บาท เก็บทั้งห้องชุดและร้านค้า (ครั้งเดียว)
    if not db.execute("SELECT 1 FROM settings WHERE key='_meter_fee_all'").fetchone():
        if db.execute("SELECT 1 FROM settings WHERE key='_shop_charges_seeded'").fetchone():
            fee = db.execute("SELECT id FROM charge_types WHERE name='ค่ารักษามิเตอร์'").fetchone()
            if fee:
                db.execute("UPDATE charge_types SET unit_type='all', active=1, description=? WHERE id=?",
                           ("ค่ารักษามิเตอร์น้ำ 25 บาทต่อเดือน (ห้องชุดและร้านค้า)", fee["id"]))
            else:
                insert_charge_type(db, {**SHOP_CHARGE_TYPES[2]})
        db.execute("INSERT INTO settings (key, value) VALUES ('_meter_fee_all', '1')")
    # เพิ่มค่าบริการร้านค้าครั้งเดียว (ถ้าแอดมินลบทิ้งภายหลังจะไม่สร้างซ้ำ)
    if not db.execute("SELECT 1 FROM settings WHERE key='_shop_charges_seeded'").fetchone():
        if not db.execute("SELECT 1 FROM charge_types WHERE unit_type='shop'").fetchone():
            for ct in SHOP_CHARGE_TYPES:
                insert_charge_type(db, ct)
        db.execute("INSERT INTO settings (key, value) VALUES ('_shop_charges_seeded', '1')")
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
