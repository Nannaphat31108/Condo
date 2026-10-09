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


def charge_applies(ct, period, unit_id, selected_unit_ids):
    if not ct["active"]:
        return False
    if ct["start_period"] and period < ct["start_period"]:
        return False
    if ct["end_period"] and period > ct["end_period"]:
        return False
    month = int(period.split("-")[1])
    if ct["frequency"] == "yearly" and month != (ct["bill_month"] or 1):
        return False
    if ct["frequency"] == "once" and period != (ct["start_period"] or period):
        return False
    if ct["apply_to"] == "selected" and unit_id not in selected_unit_ids:
        return False
    return True


def compute_item(ct, unit, reading=None, override=None):
    """คำนวณรายการค่าใช้จ่าย 1 รายการของห้อง 1 ห้อง

    คืนค่า dict ของรายการ หรือ None ถ้าเป็นค่ามิเตอร์แต่ยังไม่มีเลขมิเตอร์
    """
    method = ct["method"]
    rate = float(ct["rate"] or 0)
    min_charge = float(ct["min_charge"] or 0)
    fixed_fee = float(ct["fixed_fee"] or 0)
    detail_parts = []

    if method in METER_METHODS:
        if reading is None:
            return None
        prev, curr = float(reading["prev_reading"]), float(reading["curr_reading"])
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
        detail_parts.append(f"คิดขั้นต่ำ {fmt_num(min_charge)}")
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
    }


def compute_late_fee(settings, overdue_invoice):
    fee_type = settings.get("late_fee_type", "none")
    value = float(settings.get("late_fee_value") or 0)
    if fee_type == "fixed":
        return money(value)
    if fee_type == "percent":
        outstanding = overdue_invoice["total"] - overdue_invoice["paid_amount"]
        return money(outstanding * value / 100)
    return 0.0


def billing_dates(period, settings):
    year, month = (int(x) for x in period.split("-"))
    day = min(max(int(settings.get("issue_day") or 1), 1), calendar.monthrange(year, month)[1])
    issue = date(year, month, day)
    due = issue + timedelta(days=int(settings.get("due_days") or 15))
    return issue.isoformat(), due.isoformat()


def next_number(db, table, column, prefix):
    row = db.execute(
        f"SELECT {column} FROM {table} WHERE {column} LIKE ? ORDER BY {column} DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    seq = int(row[0][len(prefix):]) + 1 if row else 1
    return f"{prefix}{seq:04d}"


def build_unit_items(db, unit, period, settings, charge_types, selections, today=None):
    """สร้างรายการทั้งหมดของห้องในงวดนั้น คืน (items, missing_meters, overdue_ids, adhoc_ids)"""
    items, missing = [], []
    for ct in charge_types:
        sel = selections.get(ct["id"], {})
        if not charge_applies(ct, period, unit["id"], sel):
            continue
        reading = None
        if ct["method"] in METER_METHODS:
            reading = db.execute(
                "SELECT * FROM meter_readings WHERE charge_type_id=? AND unit_id=? AND period=?",
                (ct["id"], unit["id"], period),
            ).fetchone()
            if reading is None:
                missing.append(ct["name"])
                continue
        item = compute_item(ct, unit, reading, sel.get(unit["id"]))
        items.append(item)

    adhoc = db.execute(
        "SELECT * FROM adhoc_charges WHERE unit_id=? AND period<=? AND invoice_id IS NULL ORDER BY id",
        (unit["id"], period),
    ).fetchall()
    for a in adhoc:
        items.append({
            "charge_type_id": None, "description": a["description"], "detail": f"รายการเพิ่มเติม งวด {period_label(a['period'])}",
            "quantity": 1, "unit_label": "รายการ", "unit_price": a["amount"], "amount": money(a["amount"]),
            "vat_amount": 0.0, "sort_order": 900,
        })

    # ค่าปรับชำระล่าช้า ของบิลงวดก่อนที่เลยกำหนดและยังไม่ชำระครบ
    today = today or date.today().isoformat()
    overdue = db.execute(
        "SELECT * FROM invoices WHERE unit_id=? AND period<? AND status IN ('unpaid','partial') "
        "AND due_date<? AND late_fee_charged=0 ORDER BY period",
        (unit["id"], period, today),
    ).fetchall()
    overdue_ids = []
    for inv in overdue:
        fee = compute_late_fee(settings, inv)
        if fee > 0:
            items.append({
                "charge_type_id": None, "description": "ค่าปรับชำระล่าช้า",
                "detail": f"ใบแจ้งหนี้ {inv['invoice_no']} งวด {period_label(inv['period'])} ครบกำหนด {inv['due_date']}",
                "quantity": 1, "unit_label": "ครั้ง", "unit_price": fee, "amount": fee,
                "vat_amount": 0.0, "sort_order": 950,
            })
            overdue_ids.append(inv["id"])
    return items, missing, overdue_ids, [a["id"] for a in adhoc]


def load_selections(db):
    selections = {}
    for row in db.execute("SELECT * FROM charge_type_units"):
        selections.setdefault(row["charge_type_id"], {})[row["unit_id"]] = row["amount_override"]
    return selections


def generate_invoices(db, period, settings, unit_ids=None, today=None):
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
    units = db.execute(query + " ORDER BY unit_no", params).fetchall()

    result = {"created": [], "skipped_existing": [], "missing_meter": [], "empty": []}
    for unit in units:
        exists = db.execute(
            "SELECT 1 FROM invoices WHERE unit_id=? AND period=? AND status!='void'", (unit["id"], period)
        ).fetchone()
        if exists:
            result["skipped_existing"].append(unit["unit_no"])
            continue
        items, missing, overdue_ids, adhoc_ids = build_unit_items(
            db, unit, period, settings, charge_types, selections, today
        )
        if missing:
            result["missing_meter"].append(f"{unit['unit_no']} ({', '.join(missing)})")
            continue
        if not items:
            result["empty"].append(unit["unit_no"])
            continue
        subtotal = money(sum(i["amount"] for i in items))
        vat = money(sum(i["vat_amount"] for i in items))
        invoice_no = next_number(db, "invoices", "invoice_no", f"INV{period.replace('-', '')}-")
        cur = db.execute(
            "INSERT INTO invoices (invoice_no, unit_id, unit_no, owner_name, period, issue_date, due_date,"
            " subtotal, vat, total, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (invoice_no, unit["id"], unit["unit_no"], unit["owner_name"], period, issue_date, due_date,
             subtotal, vat, money(subtotal + vat), settings.get("invoice_note", "")),
        )
        invoice_id = cur.lastrowid
        for order, item in enumerate(sorted(items, key=lambda i: i["sort_order"])):
            db.execute(
                "INSERT INTO invoice_items (invoice_id, charge_type_id, description, detail, quantity, unit_label,"
                " unit_price, amount, vat_amount, sort_order) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (invoice_id, item["charge_type_id"], item["description"], item["detail"], item["quantity"],
                 item["unit_label"], item["unit_price"], item["amount"], item["vat_amount"], order),
            )
        if overdue_ids:
            db.execute(
                f"UPDATE invoices SET late_fee_charged=? WHERE id IN ({','.join('?' * len(overdue_ids))})",
                [invoice_id, *overdue_ids],
            )
        if adhoc_ids:
            db.execute(
                f"UPDATE adhoc_charges SET invoice_id=? WHERE id IN ({','.join('?' * len(adhoc_ids))})",
                [invoice_id, *adhoc_ids],
            )
        result["created"].append(invoice_no)
    db.commit()
    return result


def void_invoice(db, invoice_id):
    """ยกเลิกบิล และปล่อยรายการเพิ่มเติม/ค่าปรับให้ไปคิดในบิลใหม่ได้"""
    db.execute("UPDATE invoices SET status='void' WHERE id=?", (invoice_id,))
    db.execute("UPDATE adhoc_charges SET invoice_id=NULL WHERE invoice_id=?", (invoice_id,))
    db.execute("UPDATE invoices SET late_fee_charged=0 WHERE late_fee_charged=?", (invoice_id,))
    db.commit()


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
    db.commit()
