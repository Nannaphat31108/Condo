"""เครื่องคำนวณค่าใช้จ่ายและออกใบแจ้งหนี้อัตโนมัติ"""
import calendar
import json
from datetime import date, timedelta

METHOD_LABELS = {
    "meter_rate": "ตามมิเตอร์ (อัตราเดียว)",
    "meter_tiered": "ตามมิเตอร์ (อัตราขั้นบันได)",
    "fixed": "เหมาจ่ายคงที่ต่อห้อง",
    "per_area": "ตามพื้นที่ห้อง (ต่อ ตร.ม.)",
}
METER_METHODS = ("meter_rate", "meter_tiered")
FLAT_RATE_LABEL = "เหมาจ่าย"  # ใช้น้อยกว่าค่าขั้นต่ำ
UNIT_TYPE_LABELS = {"room": "ห้องชุด", "shop": "ร้านค้าหน้าอาคาร"}
CHARGE_UNIT_TYPE_LABELS = {"all": "ทุกประเภท", "room": "ห้องชุด", "shop": "ร้านค้า"}
FREQUENCY_LABELS = {"monthly": "ทุกเดือน", "yearly": "ปีละครั้ง", "once": "ครั้งเดียว"}
STATUS_LABELS = {"unpaid": "ค้างชำระ", "partial": "ชำระบางส่วน", "paid": "ชำระแล้ว", "void": "ยกเลิก"}
THAI_MONTHS = ["", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
               "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]


def money(value):
    return round(float(value or 0) + 1e-9, 2)


def period_label(period):
    """'2026-10' -> 'ตุลาคม 2569'"""
    try:
        year, month = (int(x) for x in period.split("-"))
        return f"{THAI_MONTHS[month]} {year + 543}"
    except (ValueError, AttributeError, IndexError):
        return period or ""


def prev_period(period):
    year, month = (int(x) for x in period.split("-"))
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


def current_period():
    today = date.today()
    return f"{today.year}-{today.month:02d}"


def parse_tiers(raw):
    """แปลง tiers เป็นรายการ [{'upto': float|None, 'rate': float}] เรียงจากน้อยไปมาก"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except ValueError:
            raw = []
    tiers = []
    for t in raw or []:
        upto = t.get("upto")
        tiers.append({"upto": None if upto in (None, "") else float(upto), "rate": float(t.get("rate") or 0)})
    tiers.sort(key=lambda t: float("inf") if t["upto"] is None else t["upto"])
    return tiers


def calc_tiered(usage, tiers):
    """คิดแบบขั้นบันได (progressive) คืนค่า (ยอดเงิน, รายละเอียดแต่ละขั้น)"""
    total, lower, parts = 0.0, 0.0, []
    for i, tier in enumerate(tiers):
        upper = tier["upto"]
        is_last = i == len(tiers) - 1
        top = usage if (upper is None or is_last) else min(usage, upper)
        qty = top - lower
        if qty <= 0:
            break
        total += qty * tier["rate"]
        parts.append(f"{fmt_num(lower)}-{fmt_num(top)}: {fmt_num(qty)}×{fmt_num(tier['rate'])}")
        if upper is None or usage <= upper:
            break
        lower = upper
    return total, parts


def fmt_num(value):
    value = float(value)
    return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"


def charge_applies(ct, period, unit, selections):
    """ค่าบริการนี้ต้องเก็บจากห้อง/ร้านนี้ในงวดนี้หรือไม่ (selections = {unit_id: ยอดเฉพาะห้อง})"""
    if not ct["active"]:
        return False
    if ct["unit_type"] not in ("all", None) and ct["unit_type"] != unit["unit_type"]:
        return False
    if ct["manual_amount"] and not selections.get(unit["id"]):
        return False  # กรอกยอดเองรายห้อง: ห้องที่ไม่ได้กรอกยอดไม่ต้องเก็บ
    if ct["start_period"] and period < ct["start_period"]:
        return False
    if ct["end_period"] and period > ct["end_period"]:
        return False
    month = int(period.split("-")[1])
    if ct["frequency"] == "yearly" and month != (ct["bill_month"] or 1):
        return False
    if ct["frequency"] == "once" and period != (ct["start_period"] or period):
        return False
    if ct["apply_to"] == "selected" and unit["id"] not in selections:
        return False
    return True


def compute_item(ct, unit, reading=None, override=None):
    """คำนวณรายการค่าใช้จ่าย 1 รายการของห้อง 1 ห้อง

    คืนค่า dict ของรายการ หรือ None ถ้าเป็นค่ามิเตอร์แต่ยังไม่มีเลขมิเตอร์
    """
    method = ct["method"]
    rate = float(ct["rate"] or 0)
    meter_prev = meter_curr = None
    min_charge = float(ct["min_charge"] or 0)
    fixed_fee = float(ct["fixed_fee"] or 0)
    detail_parts = []

    if method in METER_METHODS:
        if reading is None:
            return None
        prev, curr = float(reading["prev_reading"]), float(reading["curr_reading"])
        meter_prev, meter_curr = prev, curr
        usage = max(curr - prev, 0)
        quantity = usage
        detail_parts.append(f"เลขมิเตอร์ {fmt_num(prev)} → {fmt_num(curr)} ใช้ {fmt_num(usage)} {ct['unit_label'] or ''}".strip())
        if method == "meter_rate":
            base = usage * rate
            unit_price = rate
        else:
            base, parts = calc_tiered(usage, parse_tiers(ct["tiers"]))
            detail_parts.append("ขั้นบันได " + ", ".join(parts))
            unit_price = base / usage if usage else 0
        if fixed_fee:
            detail_parts.append(f"ค่าบริการ {fmt_num(fixed_fee)}")
        amount = base + fixed_fee
    elif method == "per_area":
        area = float(unit["area_sqm"] or 0)
        quantity, unit_price = area, rate
        amount = area * rate + fixed_fee
        detail_parts.append(f"พื้นที่ {fmt_num(area)} ตร.ม. × {fmt_num(rate)}")
    else:  # fixed
        quantity, unit_price = 1, rate
        amount = rate + fixed_fee

    if override is not None:
        amount = float(override)
        detail_parts.append("ยอดเฉพาะห้อง")
    elif min_charge and amount < min_charge:
        detail_parts.append(f"{FLAT_RATE_LABEL} {fmt_num(min_charge)}")
        amount = min_charge

    amount = money(amount)
    vat_amount = money(amount * float(ct["vat_percent"] or 0) / 100)
    return {
        "charge_type_id": ct["id"],
        "description": ct["name"],
        "detail": " | ".join(detail_parts),
        "quantity": round(quantity, 4),
        "unit_label": ct["unit_label"] or "",
        "unit_price": round(unit_price, 4),
        "amount": amount,
        "vat_amount": vat_amount,
        "sort_order": ct["sort_order"],
        "kind": "auto",
        "meter_prev": meter_prev,
        "meter_curr": meter_curr,
    }


def billing_dates(period, settings):
    year, month = (int(x) for x in period.split("-"))
    day = min(max(int(settings.get("issue_day") or 1), 1), calendar.monthrange(year, month)[1])
    issue = date(year, month, day)
    due = issue + timedelta(days=int(settings.get("due_days") or 15))
    return issue.isoformat(), due.isoformat()


def next_number(db, table, column, period):
    """เลขที่เอกสารรันใหม่ทุกเดือน รูปแบบ 0001/10/2026 (ลำดับ/เดือน/ปี)

    period คือ 'YYYY-MM' หรือวันที่ 'YYYY-MM-DD' ของเดือนที่ออกเอกสาร
    """
    year, month = period[:4], period[5:7]
    suffix = f"/{month}/{year}"
    rows = db.execute(f"SELECT {column} FROM {table} WHERE {column} LIKE ?", ("%" + suffix,)).fetchall()
    seq = max((int(r[0].split("/")[0]) for r in rows if r[0].split("/")[0].isdigit()), default=0) + 1
    return f"{seq:04d}{suffix}"


def charge_item(db, ct, unit, period, selections):
    """รายการของค่าบริการ 1 ตัวสำหรับห้องนี้ คืน item หรือ None ถ้าเป็นค่ามิเตอร์ที่ยังไม่ได้จด"""
    sel = selections.get(ct["id"], {})
    reading = None
    if ct["method"] in METER_METHODS:
        reading = db.execute(
            "SELECT * FROM meter_readings WHERE charge_type_id=? AND unit_id=? AND period=?",
            (ct["id"], unit["id"], period),
        ).fetchone()
        if reading is None:
            return None
    item = compute_item(ct, unit, reading, sel.get(unit["id"]))
    if reading is not None:
        prev = db.execute(
            "SELECT read_date, recorded_at FROM meter_readings WHERE charge_type_id=? AND unit_id=? AND period<?"
            " ORDER BY period DESC LIMIT 1", (ct["id"], unit["id"], period),
        ).fetchone()
        item["meter_curr_date"] = reading["read_date"] or reading["recorded_at"][:10]
        item["meter_prev_date"] = (prev["read_date"] or prev["recorded_at"][:10]) if prev else None
    return item


def load_exclusions(db, unit_id, period):
    """ค่าบริการที่ติ๊กออกสำหรับห้องนี้ในงวดนี้ (ไม่เรียกเก็บ เช่น จ่ายค่าน้ำมาแล้ว)"""
    return {r[0] for r in db.execute(
        "SELECT charge_type_id FROM bill_exclusions WHERE unit_id=? AND period=?", (unit_id, period))}


def build_unit_items(db, unit, period, charge_types, selections):
    """สร้างรายการทั้งหมดของห้องในงวดนั้น คืน (items, missing_meters, adhoc_ids)"""
    items, missing = [], []
    excluded = load_exclusions(db, unit["id"], period)
    for ct in charge_types:
        if ct["id"] in excluded or not charge_applies(ct, period, unit, selections.get(ct["id"], {})):
            continue
        item = charge_item(db, ct, unit, period, selections)
        if item is None:
            missing.append(ct["name"])
            continue
        items.append(item)

    adhoc = db.execute(
        "SELECT * FROM adhoc_charges WHERE unit_id=? AND period<=? AND invoice_id IS NULL ORDER BY id",
        (unit["id"], period),
    ).fetchall()
    for a in adhoc:
        penalty = a["kind"] == "penalty"
        items.append({
            "charge_type_id": None, "description": a["description"],
            "detail": "" if penalty else f"รายการเพิ่มเติม งวด {period_label(a['period'])}",
            "quantity": 1, "unit_label": "รายการ", "unit_price": a["amount"], "amount": money(a["amount"]),
            "vat_amount": 0.0, "sort_order": 950 if penalty else 900, "kind": "penalty" if penalty else "manual",
            "meter_prev": None, "meter_curr": None,
        })
    return items, missing, [a["id"] for a in adhoc]


def load_selections(db):
    selections = {}
    for row in db.execute("SELECT * FROM charge_type_units"):
        selections.setdefault(row["charge_type_id"], {})[row["unit_id"]] = row["amount_override"]
    return selections


ITEM_COLUMNS = ("charge_type_id", "description", "detail", "quantity", "unit_label", "unit_price", "amount",
                "vat_amount", "sort_order", "kind", "meter_prev", "meter_curr", "meter_prev_date", "meter_curr_date")


def insert_item(db, invoice_id, item):
    db.execute(
        f"INSERT INTO invoice_items (invoice_id, {', '.join(ITEM_COLUMNS)}) VALUES (?{', ?' * len(ITEM_COLUMNS)})",
        [invoice_id, *(item.get(c) for c in ITEM_COLUMNS)],
    )


def generate_invoices(db, period, settings, unit_ids=None):
    """ออกใบแจ้งหนี้ให้ทุกห้อง (ที่ยังไม่มีบิลในงวดนี้)

    ห้องที่ยังไม่ได้จดมิเตอร์จะถูกข้ามไว้ก่อน เพื่อไม่ให้บิลผิด
    """
    charge_types = db.execute("SELECT * FROM charge_types ORDER BY sort_order, id").fetchall()
    selections = load_selections(db)
    issue_date, due_date = billing_dates(period, settings)
    query = "SELECT * FROM units WHERE active=1"
    params = []
    if unit_ids:
        query += f" AND id IN ({','.join('?' * len(unit_ids))})"
        params = list(unit_ids)
    units = db.execute(query + " ORDER BY length(unit_no), unit_no", params).fetchall()

    result = {"created": [], "skipped_existing": [], "missing_meter": [], "empty": []}
    for unit in units:
        exists = db.execute(
            "SELECT 1 FROM invoices WHERE unit_id=? AND period=? AND status!='void'", (unit["id"], period)
        ).fetchone()
        if exists:
            result["skipped_existing"].append(unit["unit_no"])
            continue
        items, missing, adhoc_ids = build_unit_items(db, unit, period, charge_types, selections)
        if missing:
            result["missing_meter"].append(f"{unit['unit_no']} ({', '.join(missing)})")
            continue
        if not items:
            result["empty"].append(unit["unit_no"])
            continue
        subtotal = money(sum(i["amount"] for i in items))
        vat = money(sum(i["vat_amount"] for i in items))
        invoice_no = next_number(db, "invoices", "invoice_no", period)
        cur = db.execute(
            "INSERT INTO invoices (invoice_no, unit_id, unit_no, owner_name, tenant_name, period, issue_date, due_date,"
            " subtotal, vat, total, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (invoice_no, unit["id"], unit["unit_no"], unit["owner_name"], unit["tenant_name"] or "", period,
             issue_date, due_date,
             subtotal, vat, money(subtotal + vat), settings.get("invoice_note", "")),
        )
        invoice_id = cur.lastrowid
        for order, item in enumerate(sorted(items, key=lambda i: i["sort_order"])):
            insert_item(db, invoice_id, {**item, "sort_order": order})
        if adhoc_ids:
            db.execute(
                f"UPDATE adhoc_charges SET invoice_id=? WHERE id IN ({','.join('?' * len(adhoc_ids))})",
                [invoice_id, *adhoc_ids],
            )
        result["created"].append(invoice_no)
    db.commit()
    return result


def void_invoice(db, invoice_id):
    """ยกเลิกบิล และปล่อยรายการเพิ่มเติมให้ไปคิดในบิลใหม่ได้"""
    db.execute("UPDATE invoices SET status='void' WHERE id=?", (invoice_id,))
    db.execute("UPDATE adhoc_charges SET invoice_id=NULL WHERE invoice_id=?", (invoice_id,))
    db.commit()


def invoice_editable(inv):
    return inv is not None and inv["status"] in ("unpaid", "partial")


def renumber_items(db, invoice_id):
    """เรียงลำดับรายการในบิลใหม่ตามลำดับค่าบริการ (หลังเพิ่ม/ลบรายการ)"""
    rows = db.execute(
        "SELECT ii.id FROM invoice_items ii LEFT JOIN charge_types c ON c.id=ii.charge_type_id WHERE ii.invoice_id=?"
        " ORDER BY COALESCE(c.sort_order, CASE ii.kind WHEN 'penalty' THEN 950 ELSE 900 END), ii.id",
        (invoice_id,)).fetchall()
    for order, row in enumerate(rows):
        db.execute("UPDATE invoice_items SET sort_order=? WHERE id=?", (order, row["id"]))


def recalc_invoice(db, invoice_id):
    """คำนวณยอดรวมบิลใหม่หลังเพิ่ม/แก้/ลบรายการ"""
    row = db.execute(
        "SELECT COALESCE(SUM(amount),0), COALESCE(SUM(vat_amount),0) FROM invoice_items WHERE invoice_id=?",
        (invoice_id,),
    ).fetchone()
    subtotal, vat = money(row[0]), money(row[1])
    db.execute("UPDATE invoices SET subtotal=?, vat=?, total=? WHERE id=?",
               (subtotal, vat, money(subtotal + vat), invoice_id))
    refresh_invoice_status(db, invoice_id)


def set_penalty(db, invoice_id, name, amount):
    """ใส่/แก้/ลบ เบี้ยปรับที่แอดมินกรอกเอง (amount = 0 คือลบออก)"""
    amount = money(amount)
    existing = db.execute(
        "SELECT id, amount FROM invoice_items WHERE invoice_id=? AND kind='penalty' AND description=?",
        (invoice_id, name),
    ).fetchone()
    if existing and existing["amount"] == amount:
        return False
    if existing and amount == 0:
        db.execute("DELETE FROM invoice_items WHERE id=?", (existing["id"],))
    elif existing:
        db.execute("UPDATE invoice_items SET amount=?, unit_price=? WHERE id=?", (amount, amount, existing["id"]))
    elif amount:
        insert_item(db, invoice_id, {
            "description": name, "detail": "", "quantity": 1, "unit_label": "", "unit_price": amount,
            "amount": amount, "vat_amount": 0.0, "sort_order": 950, "kind": "penalty",
        })
    else:
        return False
    recalc_invoice(db, invoice_id)
    return True


def add_manual_item(db, invoice_id, description, amount):
    amount = money(amount)
    insert_item(db, invoice_id, {
        "description": description, "detail": "", "quantity": 1, "unit_label": "", "unit_price": amount,
        "amount": amount, "vat_amount": 0.0, "sort_order": 900, "kind": "manual",
    })
    recalc_invoice(db, invoice_id)


def refresh_invoice_status(db, invoice_id):
    inv = db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if inv is None or inv["status"] == "void":
        return
    paid = money(db.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE invoice_id=?", (invoice_id,)
    ).fetchone()[0])
    if paid <= 0:
        status = "unpaid"
    elif paid + 0.005 >= inv["total"]:
        status = "paid"
    else:
        status = "partial"
    db.execute("UPDATE invoices SET paid_amount=?, status=? WHERE id=?", (paid, status, invoice_id))


THAI_DIGITS = ["ศูนย์", "หนึ่ง", "สอง", "สาม", "สี่", "ห้า", "หก", "เจ็ด", "แปด", "เก้า"]
THAI_PLACES = ["", "สิบ", "ร้อย", "พัน", "หมื่น", "แสน"]


def _thai_number(n, whole):
    if n >= 1_000_000:
        rest = n % 1_000_000
        return _thai_number(n // 1_000_000, n // 1_000_000) + "ล้าน" + (_thai_number(rest, whole) if rest else "")
    text, digits = "", str(n)
    for i, ch in enumerate(digits):
        d, place = int(ch), len(digits) - i - 1
        if d == 0:
            continue
        if place == 1 and d == 1:
            text += "สิบ"
        elif place == 1 and d == 2:
            text += "ยี่สิบ"
        elif place == 0 and d == 1 and whole > 1:
            text += "เอ็ด"
        else:
            text += THAI_DIGITS[d] + THAI_PLACES[place]
    return text


def bahttext(amount):
    """1250.50 -> 'หนึ่งพันสองร้อยห้าสิบบาทห้าสิบสตางค์'"""
    amount = money(amount)
    sign = "ลบ" if amount < 0 else ""
    satang_total = round(abs(amount) * 100)
    baht, satang = divmod(satang_total, 100)
    if baht == 0 and satang == 0:
        return "ศูนย์บาทถ้วน"
    text = (_thai_number(baht, baht) + "บาท") if baht else ""
    text += (_thai_number(satang, satang) + "สตางค์") if satang else "ถ้วน"
    return sign + text


THAI_MONTHS_SHORT = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
                     "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]


def thai_date_short(value):
    """'2026-10-08' -> '8 ต.ค. 69'"""
    try:
        d = date.fromisoformat(str(value)[:10])
    except ValueError:
        return value or ""
    return f"{d.day} {THAI_MONTHS_SHORT[d.month]} {(d.year + 543) % 100:02d}"


def thai_date(value):
    """'2026-10-09' -> '9 ตุลาคม 2569'"""
    try:
        d = date.fromisoformat(str(value)[:10])
    except ValueError:
        return value or ""
    return f"{d.day} {THAI_MONTHS[d.month]} {d.year + 543}"
