"""หน้าจอสำหรับผู้ดูแลระบบ (นิติบุคคล)"""
import csv
import io
import json
import sqlite3
from datetime import date

from flask import (Blueprint, Response, abort, flash, g, redirect, render_template, request,
                   send_file, url_for)
from werkzeug.security import generate_password_hash

from . import billing
from .auth import MIN_PASSWORD_LENGTH, admin_required
from .db import DEFAULT_SETTINGS, get_db, get_settings, log_activity

bp = Blueprint("admin", __name__, url_prefix="/admin")


def to_float(value, default=0.0):
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def valid_period(value):
    try:
        year, month = (int(x) for x in (value or "").split("-"))
        return 2000 <= year <= 2600 and 1 <= month <= 12
    except ValueError:
        return False


def get_period_arg(name="period"):
    period = request.values.get(name, "")
    return period if valid_period(period) else billing.current_period()


def get_or_404(sql, params):
    row = get_db().execute(sql, params).fetchone()
    if row is None:
        abort(404)
    return row


# ---------------------------------------------------------------- dashboard
@bp.route("/")
@admin_required
def dashboard():
    db = get_db()
    period = get_period_arg()
    stats = db.execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(total),0) AS total, COALESCE(SUM(paid_amount),0) AS paid,"
        " SUM(status='paid') AS paid_cnt FROM invoices WHERE period=? AND status!='void'",
        (period,),
    ).fetchone()
    outstanding = db.execute(
        "SELECT COALESCE(SUM(total-paid_amount),0) AS amount, COUNT(*) AS cnt FROM invoices"
        " WHERE status IN ('unpaid','partial')"
    ).fetchone()
    overdue = db.execute(
        "SELECT * FROM invoices WHERE status IN ('unpaid','partial') AND due_date<? ORDER BY due_date LIMIT 10",
        (date.today().isoformat(),),
    ).fetchall()
    unit_count = db.execute("SELECT COUNT(*) FROM units WHERE active=1").fetchone()[0]
    monthly = db.execute(
        "SELECT period, SUM(total) AS total, SUM(paid_amount) AS paid FROM invoices WHERE status!='void'"
        " GROUP BY period ORDER BY period DESC LIMIT 12"
    ).fetchall()[::-1]
    peak = max([m["total"] for m in monthly] + [1])
    return render_template("admin/dashboard.html", period=period, stats=stats, outstanding=outstanding,
                           overdue=overdue, unit_count=unit_count, monthly=monthly, peak=peak)


# ---------------------------------------------------------------- units
UNIT_FIELDS = ("unit_no", "building", "floor", "area_sqm", "owner_name", "phone", "email", "note")


def unit_form_data():
    data = {f: request.form.get(f, "").strip() for f in UNIT_FIELDS}
    data["area_sqm"] = to_float(data["area_sqm"])
    data["active"] = 1 if request.form.get("active") else 0
    return data


@bp.route("/units")
@admin_required
def units():
    q = request.args.get("q", "").strip()
    sql = ("SELECT u.*, (SELECT COALESCE(SUM(total-paid_amount),0) FROM invoices i WHERE i.unit_id=u.id"
           " AND i.status IN ('unpaid','partial')) AS outstanding,"
           " (SELECT GROUP_CONCAT(username, ', ') FROM users WHERE unit_id=u.id) AS usernames FROM units u")
    params = []
    if q:
        sql += " WHERE u.unit_no LIKE ? OR u.owner_name LIKE ? OR u.phone LIKE ?"
        params = [f"%{q}%"] * 3
    rows = get_db().execute(sql + " ORDER BY u.active DESC, u.unit_no", params).fetchall()
    return render_template("admin/units.html", units=rows, q=q)


@bp.route("/units/new", methods=("GET", "POST"))
@bp.route("/units/<int:unit_id>/edit", methods=("GET", "POST"))
@admin_required
def unit_form(unit_id=None):
    db = get_db()
    unit = get_or_404("SELECT * FROM units WHERE id=?", (unit_id,)) if unit_id else None
    if request.method == "POST":
        data = unit_form_data()
        if not data["unit_no"]:
            flash("กรุณากรอกเลขห้อง/บ้านเลขที่", "error")
        else:
            try:
                if unit:
                    db.execute(
                        "UPDATE units SET unit_no=?, building=?, floor=?, area_sqm=?, owner_name=?, phone=?,"
                        " email=?, note=?, active=? WHERE id=?",
                        (*[data[f] for f in UNIT_FIELDS], data["active"], unit_id),
                    )
                    log_activity(g.user, f"แก้ไขห้อง {data['unit_no']}")
                else:
                    db.execute(
                        "INSERT INTO units (unit_no, building, floor, area_sqm, owner_name, phone, email, note, active)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (*[data[f] for f in UNIT_FIELDS], data["active"]),
                    )
                    log_activity(g.user, f"เพิ่มห้อง {data['unit_no']}")
                db.commit()
                flash("บันทึกข้อมูลห้องเรียบร้อย", "success")
                return redirect(url_for("admin.units"))
            except sqlite3.IntegrityError:
                flash("เลขห้องนี้มีอยู่แล้ว", "error")
        unit = {**(dict(unit) if unit else {}), **data}
    return render_template("admin/unit_form.html", unit=unit, unit_id=unit_id)


@bp.route("/units/import", methods=("GET", "POST"))
@admin_required
def unit_import():
    """นำเข้าห้องจำนวนมาก: สร้างตามชั้น/จำนวนห้อง หรือจากข้อความ CSV เลขห้อง,อาคาร,ชั้น,พื้นที่,ชื่อเจ้าของ,โทร"""
    if request.method == "POST":
        db = get_db()
        added, skipped = 0, []
        if request.form.get("mode") == "generate":
            first = request.form.get("floor_from", type=int) or 1
            last = request.form.get("floor_to", type=int) or first
            per_floor = request.form.get("per_floor", type=int) or 0
            digits = request.form.get("digits", type=int) or 2
            prefix = request.form.get("prefix", "").strip()
            building = request.form.get("building", "").strip()
            area = to_float(request.form.get("area_sqm"))
            if not (1 <= per_floor <= 200 and 0 <= first <= last <= first + 200):
                flash("กรุณากรอกชั้นและจำนวนห้องต่อชั้นให้ถูกต้อง", "error")
                return redirect(url_for("admin.unit_import"))
            for floor in range(first, last + 1):
                for room in range(1, per_floor + 1):
                    unit_no = f"{prefix}{floor}{room:0{digits}d}"
                    try:
                        db.execute("INSERT INTO units (unit_no, building, floor, area_sqm) VALUES (?,?,?,?)",
                                   (unit_no, building, str(floor), area))
                        added += 1
                    except sqlite3.IntegrityError:
                        skipped.append(unit_no)
            log_activity(g.user, f"สร้างห้องอัตโนมัติ {added} ห้อง")
            db.commit()
            flash(f"สร้างห้องเรียบร้อย {added} ห้อง" + (f" (ข้ามเลขห้องซ้ำ {len(skipped)} ห้อง)" if skipped else ""),
                  "success")
            return redirect(url_for("admin.units"))
        text = request.form.get("csv_text", "")
        upload = request.files.get("csv_file")
        if upload and upload.filename:
            text = upload.read().decode("utf-8-sig", errors="replace")
        for row in csv.reader(io.StringIO(text)):
            row = [c.strip() for c in row] + [""] * 6
            if not row[0] or row[0] in ("unit_no", "เลขห้อง"):
                continue
            try:
                db.execute(
                    "INSERT INTO units (unit_no, building, floor, area_sqm, owner_name, phone) VALUES (?,?,?,?,?,?)",
                    (row[0], row[1], row[2], to_float(row[3]), row[4], row[5]),
                )
                added += 1
            except sqlite3.IntegrityError:
                skipped.append(row[0])
        log_activity(g.user, f"นำเข้าห้อง {added} ห้อง")
        db.commit()
        flash(f"นำเข้าเรียบร้อย {added} ห้อง" + (f" (ข้ามเลขห้องซ้ำ: {', '.join(skipped)})" if skipped else ""),
              "success")
        return redirect(url_for("admin.units"))
    return render_template("admin/unit_import.html")


@bp.route("/units/<int:unit_id>")
@admin_required
def unit_detail(unit_id):
    db = get_db()
    unit = get_or_404("SELECT * FROM units WHERE id=?", (unit_id,))
    invoices = db.execute("SELECT * FROM invoices WHERE unit_id=? ORDER BY period DESC, id DESC", (unit_id,)).fetchall()
    readings = db.execute(
        "SELECT m.*, c.name, c.unit_label FROM meter_readings m JOIN charge_types c ON c.id=m.charge_type_id"
        " WHERE m.unit_id=? ORDER BY m.period DESC, c.sort_order LIMIT 48",
        (unit_id,),
    ).fetchall()
    users = db.execute("SELECT * FROM users WHERE unit_id=?", (unit_id,)).fetchall()
    return render_template("admin/unit_detail.html", unit=unit, invoices=invoices, readings=readings, users=users)


@bp.route("/units/<int:unit_id>/delete", methods=("POST",))
@admin_required
def unit_delete(unit_id):
    db = get_db()
    unit = get_or_404("SELECT * FROM units WHERE id=?", (unit_id,))
    if db.execute("SELECT 1 FROM invoices WHERE unit_id=?", (unit_id,)).fetchone():
        flash("ห้องนี้มีประวัติใบแจ้งหนี้แล้ว ลบไม่ได้ (ให้ปิดการใช้งานแทน เพื่อเก็บข้อมูลย้อนหลัง)", "error")
        return redirect(url_for("admin.unit_detail", unit_id=unit_id))
    db.execute("DELETE FROM units WHERE id=?", (unit_id,))
    log_activity(g.user, f"ลบห้อง {unit['unit_no']}")
    db.commit()
    flash("ลบห้องเรียบร้อย", "success")
    return redirect(url_for("admin.units"))


# ---------------------------------------------------------------- users
@bp.route("/users")
@admin_required
def users():
    rows = get_db().execute(
        "SELECT u.*, un.unit_no FROM users u LEFT JOIN units un ON un.id=u.unit_id ORDER BY u.role, u.username"
    ).fetchall()
    return render_template("admin/users.html", users=rows)


@bp.route("/users/new", methods=("GET", "POST"))
@bp.route("/users/<int:user_id>/edit", methods=("GET", "POST"))
@admin_required
def user_form(user_id=None):
    db = get_db()
    user = get_or_404("SELECT * FROM users WHERE id=?", (user_id,)) if user_id else None
    units_list = db.execute("SELECT id, unit_no, owner_name FROM units WHERE active=1 ORDER BY unit_no").fetchall()
    if request.method == "POST":
        data = {
            "username": request.form.get("username", "").strip(),
            "full_name": request.form.get("full_name", "").strip(),
            "role": request.form.get("role") if request.form.get("role") in ("admin", "resident") else "resident",
            "unit_id": request.form.get("unit_id", type=int) or None,
            "active": 1 if request.form.get("active") else 0,
        }
        password = request.form.get("password", "")
        if data["role"] == "admin":
            data["unit_id"] = None
        error = None
        if not data["username"]:
            error = "กรุณากรอกชื่อผู้ใช้"
        elif (not user or password) and len(password) < MIN_PASSWORD_LENGTH:
            error = f"รหัสผ่านต้องมีอย่างน้อย {MIN_PASSWORD_LENGTH} ตัวอักษร"
        elif data["role"] == "resident" and not data["unit_id"]:
            error = "ผู้ใช้ประเภทลูกบ้านต้องเลือกห้อง"
        elif user and user["id"] == g.user["id"] and (data["role"] != "admin" or not data["active"]):
            error = "ไม่สามารถลดสิทธิ์หรือปิดบัญชีของตัวเองได้"
        if error:
            flash(error, "error")
            user = {**(dict(user) if user else {}), **data}
        else:
            try:
                if user:
                    db.execute("UPDATE users SET username=?, full_name=?, role=?, unit_id=?, active=? WHERE id=?",
                               (data["username"], data["full_name"], data["role"], data["unit_id"], data["active"],
                                user_id))
                    if password:
                        db.execute("UPDATE users SET password_hash=? WHERE id=?",
                                   (generate_password_hash(password), user_id))
                    log_activity(g.user, f"แก้ไขผู้ใช้ {data['username']}")
                else:
                    db.execute(
                        "INSERT INTO users (username, password_hash, full_name, role, unit_id, active)"
                        " VALUES (?,?,?,?,?,?)",
                        (data["username"], generate_password_hash(password), data["full_name"], data["role"],
                         data["unit_id"], data["active"]),
                    )
                    log_activity(g.user, f"เพิ่มผู้ใช้ {data['username']}")
                db.commit()
                flash("บันทึกผู้ใช้เรียบร้อย", "success")
                return redirect(url_for("admin.users"))
            except sqlite3.IntegrityError:
                flash("ชื่อผู้ใช้นี้มีอยู่แล้ว", "error")
                user = {**(dict(user) if user else {}), **data}
    if user is None and request.args.get("unit_id", type=int):
        user = {"role": "resident", "unit_id": request.args.get("unit_id", type=int), "active": 1}
    return render_template("admin/user_form.html", user=user, user_id=user_id, units=units_list)


@bp.route("/users/<int:user_id>/delete", methods=("POST",))
@admin_required
def user_delete(user_id):
    if user_id == g.user["id"]:
        flash("ไม่สามารถลบบัญชีของตัวเองได้", "error")
        return redirect(url_for("admin.users"))
    db = get_db()
    user = get_or_404("SELECT * FROM users WHERE id=?", (user_id,))
    db.execute("UPDATE payments SET created_by=NULL WHERE created_by=?", (user_id,))
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    log_activity(g.user, f"ลบผู้ใช้ {user['username']}")
    db.commit()
    flash("ลบผู้ใช้เรียบร้อย", "success")
    return redirect(url_for("admin.users"))


# ---------------------------------------------------------------- charge types (วิธีคิดค่าใช้จ่าย)
@bp.route("/charges")
@admin_required
def charges():
    db = get_db()
    rows = db.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM charge_type_units WHERE charge_type_id=c.id) AS unit_count"
        " FROM charge_types c ORDER BY c.active DESC, c.sort_order, c.id"
    ).fetchall()
    return render_template("admin/charges.html", charges=rows, parse_tiers=billing.parse_tiers)


@bp.route("/charges/new", methods=("GET", "POST"))
@bp.route("/charges/<int:charge_id>/edit", methods=("GET", "POST"))
@admin_required
def charge_form(charge_id=None):
    db = get_db()
    ct = get_or_404("SELECT * FROM charge_types WHERE id=?", (charge_id,)) if charge_id else None
    units_list = db.execute("SELECT * FROM units WHERE active=1 ORDER BY unit_no").fetchall()
    selections = {}
    if charge_id:
        selections = {r["unit_id"]: r["amount_override"] for r in
                      db.execute("SELECT * FROM charge_type_units WHERE charge_type_id=?", (charge_id,))}

    if request.method == "POST":
        f = request.form
        tiers = []
        for upto, rate in zip(f.getlist("tier_upto"), f.getlist("tier_rate")):
            if str(rate).strip() == "":
                continue
            tiers.append({"upto": to_float(upto) if str(upto).strip() else None, "rate": to_float(rate)})
        data = {
            "name": f.get("name", "").strip(),
            "description": f.get("description", "").strip(),
            "method": f.get("method") if f.get("method") in billing.METHOD_LABELS else "fixed",
            "rate": to_float(f.get("rate")),
            "tiers": json.dumps(billing.parse_tiers(tiers), ensure_ascii=False),
            "fixed_fee": to_float(f.get("fixed_fee")),
            "min_charge": to_float(f.get("min_charge")),
            "unit_label": f.get("unit_label", "").strip(),
            "vat_percent": to_float(f.get("vat_percent")),
            "frequency": f.get("frequency") if f.get("frequency") in billing.FREQUENCY_LABELS else "monthly",
            "bill_month": f.get("bill_month", type=int),
            "start_period": f.get("start_period") if valid_period(f.get("start_period")) else None,
            "end_period": f.get("end_period") if valid_period(f.get("end_period")) else None,
            "apply_to": "selected" if f.get("apply_to") == "selected" else "all",
            "sort_order": f.get("sort_order", type=int) or 0,
            "active": 1 if f.get("active") else 0,
        }
        new_sel = {}
        for unit in units_list:
            chosen = f.get(f"unit_{unit['id']}")
            override = f.get(f"override_{unit['id']}", "").strip()
            if chosen or override:
                new_sel[unit["id"]] = to_float(override) if override else None

        error = None
        if not data["name"]:
            error = "กรุณากรอกชื่อค่าใช้จ่าย"
        elif data["method"] == "meter_tiered" and not json.loads(data["tiers"]):
            error = "กรุณากำหนดอัตราขั้นบันไดอย่างน้อย 1 ขั้น"
        elif data["frequency"] == "once" and not data["start_period"]:
            error = "ค่าบริการแบบครั้งเดียว ต้องระบุงวดที่เรียกเก็บ (งวดเริ่มต้น)"
        elif data["apply_to"] == "selected" and not new_sel:
            error = "กรุณาเลือกห้องที่ใช้ค่าบริการนี้อย่างน้อย 1 ห้อง"
        if error:
            flash(error, "error")
            ct = {**(dict(ct) if ct else {}), **data}
            selections = new_sel
        else:
            cols = list(data)
            if charge_id:
                db.execute(f"UPDATE charge_types SET {', '.join(c + '=?' for c in cols)} WHERE id=?",
                           [*data.values(), charge_id])
                log_activity(g.user, f"แก้ไขค่าบริการ {data['name']}")
            else:
                cur = db.execute(f"INSERT INTO charge_types ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                 list(data.values()))
                charge_id = cur.lastrowid
                log_activity(g.user, f"เพิ่มค่าบริการ {data['name']}")
            db.execute("DELETE FROM charge_type_units WHERE charge_type_id=?", (charge_id,))
            for unit_id, override in new_sel.items():
                db.execute("INSERT INTO charge_type_units (charge_type_id, unit_id, amount_override) VALUES (?,?,?)",
                           (charge_id, unit_id, override))
            db.commit()
            flash("บันทึกการตั้งค่าค่าบริการเรียบร้อย", "success")
            return redirect(url_for("admin.charges"))

    tiers = billing.parse_tiers(ct["tiers"]) if ct else []
    return render_template("admin/charge_form.html", ct=ct, charge_id=charge_id, units=units_list,
                           selections=selections, tiers=tiers)


@bp.route("/charges/<int:charge_id>/delete", methods=("POST",))
@admin_required
def charge_delete(charge_id):
    db = get_db()
    ct = get_or_404("SELECT * FROM charge_types WHERE id=?", (charge_id,))
    # รายการในบิลเก่ายังคงอยู่ (เก็บชื่อและยอดไว้ในบิลแล้ว)
    db.execute("DELETE FROM charge_types WHERE id=?", (charge_id,))
    log_activity(g.user, f"ลบค่าบริการ {ct['name']}")
    db.commit()
    flash("ลบค่าบริการเรียบร้อย (ใบแจ้งหนี้เดิมไม่ได้รับผลกระทบ)", "success")
    return redirect(url_for("admin.charges"))


@bp.route("/charges/preview")
@admin_required
def charge_preview():
    """ทดลองคำนวณจากหน้าตั้งค่า (ใช้ผ่าน JavaScript)"""
    a = request.args
    ct = {
        "id": 0, "name": "", "method": a.get("method", "fixed"), "rate": to_float(a.get("rate")),
        "tiers": a.get("tiers", "[]"), "fixed_fee": to_float(a.get("fixed_fee")),
        "min_charge": to_float(a.get("min_charge")), "unit_label": a.get("unit_label", ""),
        "vat_percent": to_float(a.get("vat_percent")), "sort_order": 0,
    }
    usage = to_float(a.get("usage"), 10)
    unit = {"area_sqm": to_float(a.get("area"), 30)}
    item = billing.compute_item(ct, unit, {"prev_reading": 0, "curr_reading": usage})
    return {"amount": item["amount"], "vat": item["vat_amount"], "detail": item["detail"]}


# ---------------------------------------------------------------- meter readings
@bp.route("/meters", methods=("GET", "POST"))
@admin_required
def meters():
    db = get_db()
    meter_types = db.execute(
        "SELECT * FROM charge_types WHERE method IN ('meter_rate','meter_tiered') ORDER BY active DESC, sort_order, id"
    ).fetchall()
    if not meter_types:
        flash("ยังไม่มีค่าบริการที่คิดตามมิเตอร์ กรุณาเพิ่มที่หน้า 'ตั้งค่าค่าบริการ'", "error")
        return redirect(url_for("admin.charges"))
    period = get_period_arg()
    ct_id = request.values.get("charge_type_id", type=int) or meter_types[0]["id"]
    ct = next((m for m in meter_types if m["id"] == ct_id), meter_types[0])
    units_list = db.execute("SELECT * FROM units WHERE active=1 ORDER BY unit_no").fetchall()

    if request.method == "POST":
        errors, saved = [], 0
        for unit in units_list:
            prev_raw = request.form.get(f"prev_{unit['id']}", "").strip()
            curr_raw = request.form.get(f"curr_{unit['id']}", "").strip()
            if curr_raw == "":
                continue
            prev, curr = to_float(prev_raw), to_float(curr_raw)
            if curr < prev and not request.form.get(f"reset_{unit['id']}"):
                errors.append(f"ห้อง {unit['unit_no']}: เลขปัจจุบัน ({curr_raw}) น้อยกว่าเลขครั้งก่อน ({prev_raw})")
                continue
            if curr < prev:  # มิเตอร์วนรอบ/เปลี่ยนมิเตอร์ใหม่: ถือว่าเริ่มนับจาก 0
                prev = 0
            invoiced = db.execute(
                "SELECT invoice_no FROM invoices WHERE unit_id=? AND period=? AND status!='void'",
                (unit["id"], period),
            ).fetchone()
            existing = db.execute(
                "SELECT * FROM meter_readings WHERE charge_type_id=? AND unit_id=? AND period=?",
                (ct["id"], unit["id"], period),
            ).fetchone()
            if invoiced and existing and (existing["prev_reading"], existing["curr_reading"]) != (prev, curr):
                errors.append(f"ห้อง {unit['unit_no']}: ออกบิล {invoiced['invoice_no']} ไปแล้ว"
                              " ต้องยกเลิกบิลก่อนจึงแก้เลขมิเตอร์ได้")
                continue
            db.execute(
                "INSERT INTO meter_readings (charge_type_id, unit_id, period, prev_reading, curr_reading)"
                " VALUES (?,?,?,?,?) ON CONFLICT (charge_type_id, unit_id, period) DO UPDATE SET"
                " prev_reading=excluded.prev_reading, curr_reading=excluded.curr_reading,"
                " recorded_at=datetime('now','localtime')",
                (ct["id"], unit["id"], period, prev, curr),
            )
            saved += 1
        log_activity(g.user, f"บันทึกมิเตอร์ {ct['name']} งวด {period} จำนวน {saved} ห้อง")
        db.commit()
        for e in errors:
            flash(e, "error")
        flash(f"บันทึกเลขมิเตอร์ {saved} ห้อง", "success")
        return redirect(url_for("admin.meters", period=period, charge_type_id=ct["id"]))

    current = {r["unit_id"]: r for r in db.execute(
        "SELECT * FROM meter_readings WHERE charge_type_id=? AND period=?", (ct["id"], period))}
    # เลขครั้งก่อน = เลขปัจจุบันของงวดล่าสุดก่อนหน้า
    last = {r["unit_id"]: r["curr_reading"] for r in db.execute(
        "SELECT m.unit_id, m.curr_reading FROM meter_readings m WHERE m.charge_type_id=? AND m.period=("
        " SELECT MAX(period) FROM meter_readings x WHERE x.charge_type_id=m.charge_type_id"
        " AND x.unit_id=m.unit_id AND x.period<?)",
        (ct["id"], period))}
    invoiced = {r["unit_id"] for r in db.execute(
        "SELECT unit_id FROM invoices WHERE period=? AND status!='void'", (period,))}
    rows = []
    for unit in units_list:
        reading = current.get(unit["id"])
        prev = reading["prev_reading"] if reading else last.get(unit["id"], 0)
        curr = reading["curr_reading"] if reading else None
        preview = billing.compute_item(ct, unit, {"prev_reading": prev, "curr_reading": curr}) if reading else None
        rows.append({"unit": unit, "prev": prev, "curr": curr, "preview": preview,
                     "invoiced": unit["id"] in invoiced})
    return render_template("admin/meters.html", meter_types=meter_types, ct=ct, period=period, rows=rows)


# ---------------------------------------------------------------- adhoc charges
@bp.route("/adhoc", methods=("GET", "POST"))
@admin_required
def adhoc():
    db = get_db()
    period = get_period_arg()
    units_list = db.execute("SELECT * FROM units WHERE active=1 ORDER BY unit_no").fetchall()
    if request.method == "POST":
        unit_ids = [int(x) for x in request.form.getlist("unit_ids") if x.isdigit()]
        description = request.form.get("description", "").strip()
        amount = to_float(request.form.get("amount"))
        if not unit_ids or not description or amount == 0:
            flash("กรุณาเลือกห้อง กรอกรายละเอียด และจำนวนเงิน", "error")
        else:
            for unit_id in unit_ids:
                db.execute("INSERT INTO adhoc_charges (unit_id, period, description, amount) VALUES (?,?,?,?)",
                           (unit_id, period, description, amount))
            log_activity(g.user, f"เพิ่มรายการเพิ่มเติม '{description}' {len(unit_ids)} ห้อง")
            db.commit()
            flash(f"เพิ่มรายการให้ {len(unit_ids)} ห้องแล้ว จะถูกรวมในใบแจ้งหนี้งวดถัดไปที่ออก", "success")
            return redirect(url_for("admin.adhoc", period=period))
    rows = db.execute(
        "SELECT a.*, u.unit_no, i.invoice_no FROM adhoc_charges a JOIN units u ON u.id=a.unit_id"
        " LEFT JOIN invoices i ON i.id=a.invoice_id ORDER BY a.invoice_id IS NOT NULL, a.period DESC, a.id DESC"
        " LIMIT 300"
    ).fetchall()
    return render_template("admin/adhoc.html", rows=rows, units=units_list, period=period)


@bp.route("/adhoc/<int:adhoc_id>/delete", methods=("POST",))
@admin_required
def adhoc_delete(adhoc_id):
    db = get_db()
    row = get_or_404("SELECT * FROM adhoc_charges WHERE id=?", (adhoc_id,))
    if row["invoice_id"]:
        flash("รายการนี้ออกบิลแล้ว ต้องยกเลิกบิลก่อนจึงลบได้", "error")
    else:
        db.execute("DELETE FROM adhoc_charges WHERE id=?", (adhoc_id,))
        db.commit()
        flash("ลบรายการเรียบร้อย", "success")
    return redirect(url_for("admin.adhoc"))


# ---------------------------------------------------------------- billing / invoices
@bp.route("/billing", methods=("GET", "POST"))
@admin_required
def billing_page():
    db = get_db()
    period = get_period_arg()
    if request.method == "POST":
        result = billing.generate_invoices(db, period, get_settings())
        log_activity(g.user, f"ออกใบแจ้งหนี้งวด {period} จำนวน {len(result['created'])} ฉบับ")
        db.commit()
        flash(f"ออกใบแจ้งหนี้ใหม่ {len(result['created'])} ฉบับ", "success")
        if result["missing_meter"]:
            flash("ยังไม่ได้ออกบิล เพราะยังไม่จดมิเตอร์: " + ", ".join(result["missing_meter"]), "error")
        if result["empty"]:
            flash("ห้องที่ไม่มีรายการค่าใช้จ่าย: " + ", ".join(result["empty"]), "info")
        return redirect(url_for("admin.billing_page", period=period))

    meter_types = db.execute(
        "SELECT * FROM charge_types WHERE active=1 AND method IN ('meter_rate','meter_tiered')").fetchall()
    unit_count = db.execute("SELECT COUNT(*) FROM units WHERE active=1").fetchone()[0]
    meter_status = []
    for m in meter_types:
        done = db.execute(
            "SELECT COUNT(*) FROM meter_readings r JOIN units u ON u.id=r.unit_id"
            " WHERE r.charge_type_id=? AND r.period=? AND u.active=1", (m["id"], period)).fetchone()[0]
        meter_status.append({"ct": m, "done": done})
    invoices = db.execute("SELECT * FROM invoices WHERE period=? ORDER BY unit_no", (period,)).fetchall()
    pending_adhoc = db.execute("SELECT COUNT(*) FROM adhoc_charges WHERE invoice_id IS NULL AND period<=?",
                               (period,)).fetchone()[0]
    issue_date, due_date = billing.billing_dates(period, get_settings())
    return render_template("admin/billing.html", period=period, meter_status=meter_status, unit_count=unit_count,
                           invoices=invoices, pending_adhoc=pending_adhoc, issue_date=issue_date, due_date=due_date)


@bp.route("/invoices")
@admin_required
def invoices():
    db = get_db()
    status = request.args.get("status", "")
    period = request.args.get("period", "")
    q = request.args.get("q", "").strip()
    sql, params = "SELECT * FROM invoices WHERE 1=1", []
    if status == "outstanding":
        sql += " AND status IN ('unpaid','partial')"
    elif status in billing.STATUS_LABELS:
        sql += " AND status=?"
        params.append(status)
    if valid_period(period):
        sql += " AND period=?"
        params.append(period)
    if q:
        sql += " AND (unit_no LIKE ? OR owner_name LIKE ? OR invoice_no LIKE ?)"
        params += [f"%{q}%"] * 3
    rows = db.execute(sql + " ORDER BY period DESC, unit_no LIMIT 1000", params).fetchall()
    totals = {
        "total": sum(r["total"] for r in rows if r["status"] != "void"),
        "paid": sum(r["paid_amount"] for r in rows if r["status"] != "void"),
    }
    return render_template("admin/invoices.html", invoices=rows, status=status, period=period, q=q, totals=totals)


def load_invoice(invoice_id):
    db = get_db()
    inv = get_or_404("SELECT * FROM invoices WHERE id=?", (invoice_id,))
    items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY sort_order, id", (invoice_id,)).fetchall()
    payments = db.execute(
        "SELECT p.*, u.username FROM payments p LEFT JOIN users u ON u.id=p.created_by WHERE p.invoice_id=?"
        " ORDER BY p.paid_at, p.id", (invoice_id,)).fetchall()
    unit = db.execute("SELECT * FROM units WHERE id=?", (inv["unit_id"],)).fetchone()
    return inv, items, payments, unit


@bp.route("/invoices/<int:invoice_id>")
@admin_required
def invoice_detail(invoice_id):
    inv, items, payments, unit = load_invoice(invoice_id)
    return render_template("invoice.html", inv=inv, items=items, payments=payments, unit=unit, admin=True,
                           today=date.today().isoformat(), penalty_types=penalty_type_list())


PAYMENT_METHODS = ["โอนเงิน", "เงินสด", "พร้อมเพย์", "เช็ค", "บัตรเครดิต", "อื่น ๆ"]


def record_payment(db, inv, amount, paid_at, method, reference="", note=""):
    """บันทึกรับชำระ 1 รายการ ออกเลขที่ใบเสร็จ และอัปเดตสถานะบิล (ผู้เรียกต้อง commit เอง)"""
    receipt_no = billing.next_number(db, "payments", "receipt_no", paid_at)
    db.execute(
        "INSERT INTO payments (invoice_id, receipt_no, paid_at, amount, method, reference, note, created_by)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (inv["id"], receipt_no, paid_at, amount, method, reference, note, g.user["id"]),
    )
    billing.refresh_invoice_status(db, inv["id"])
    log_activity(g.user, f"รับชำระ {inv['invoice_no']} {amount:,.2f} บาท ({receipt_no})")
    return receipt_no


@bp.route("/payments/bulk", methods=("GET", "POST"))
@admin_required
def payments_bulk():
    """รับชำระหลายห้องในหน้าเดียว"""
    db = get_db()
    period = get_period_arg()
    invoices_ = db.execute(
        "SELECT * FROM invoices WHERE period=? AND status IN ('unpaid','partial') ORDER BY unit_no", (period,)
    ).fetchall()
    if request.method == "POST":
        paid_at = request.form.get("paid_at") or date.today().isoformat()
        method = request.form.get("method") if request.form.get("method") in PAYMENT_METHODS else "โอนเงิน"
        receipts, errors = [], []
        for inv in invoices_:
            if not request.form.get(f"pay_{inv['id']}"):
                continue
            amount = billing.money(to_float(request.form.get(f"amount_{inv['id']}")))
            outstanding = billing.money(inv["total"] - inv["paid_amount"])
            if amount <= 0 or amount > outstanding + 0.005:
                errors.append(inv["unit_no"])
                continue
            receipts.append(record_payment(db, inv, amount, paid_at, method,
                                           request.form.get(f"ref_{inv['id']}", "").strip()))
        db.commit()
        if receipts:
            flash(f"บันทึกรับชำระ {len(receipts)} ห้อง ใบเสร็จเลขที่ {receipts[0]} ถึง {receipts[-1]}", "success")
        if errors:
            flash("จำนวนเงินไม่ถูกต้อง (ยังไม่บันทึก): ห้อง " + ", ".join(errors), "error")
        if not receipts and not errors:
            flash("ยังไม่ได้เลือกห้องที่ชำระ", "info")
        return redirect(url_for("admin.payments_bulk", period=period))
    return render_template("admin/payments_bulk.html", period=period, invoices=invoices_,
                           methods=PAYMENT_METHODS, today=date.today().isoformat())


@bp.route("/receipts/print")
@admin_required
def receipts_print():
    """พิมพ์ใบเสร็จหลายใบในครั้งเดียว: ตามงวดบิล หรือตามเดือนที่รับเงิน"""
    db = get_db()
    period = get_period_arg()
    by = "paid" if request.args.get("by") == "paid" else "invoice"
    layout = "half" if request.args.get("layout") == "half" else "full"
    if by == "paid":
        rows = db.execute("SELECT p.* FROM payments p WHERE substr(p.paid_at,1,7)=? ORDER BY p.paid_at, p.id",
                          (period,)).fetchall()
    else:
        rows = db.execute("SELECT p.* FROM payments p JOIN invoices i ON i.id=p.invoice_id WHERE i.period=?"
                          " ORDER BY i.unit_no, p.id", (period,)).fetchall()
    docs = []
    for p in rows:
        inv, items, _payments, unit = load_invoice(p["invoice_id"])
        docs.append((p, inv, items, unit))
    return render_template("receipt_batch.html", docs=docs, period=period, by=by, layout=layout)


@bp.route("/invoices/<int:invoice_id>/pay", methods=("POST",))
@admin_required
def invoice_pay(invoice_id):
    db = get_db()
    inv = get_or_404("SELECT * FROM invoices WHERE id=?", (invoice_id,))
    amount = billing.money(to_float(request.form.get("amount")))
    paid_at = request.form.get("paid_at") or date.today().isoformat()
    if inv["status"] == "void":
        flash("บิลนี้ถูกยกเลิกแล้ว", "error")
    elif amount <= 0:
        flash("จำนวนเงินต้องมากกว่า 0", "error")
    elif amount > billing.money(inv["total"] - inv["paid_amount"]) + 0.005:
        flash("จำนวนเงินเกินยอดค้างชำระ", "error")
    else:
        receipt_no = record_payment(db, inv, amount, paid_at, request.form.get("method", "โอนเงิน"),
                                    request.form.get("reference", "").strip(), request.form.get("note", "").strip())
        db.commit()
        flash(f"บันทึกรับชำระเรียบร้อย ใบเสร็จเลขที่ {receipt_no}", "success")
    return redirect(url_for("admin.invoice_detail", invoice_id=invoice_id))


@bp.route("/invoices/<int:invoice_id>/items", methods=("POST",))
@admin_required
def invoice_add_item(invoice_id):
    """แอดมินเพิ่มรายการ/เบี้ยปรับในบิลเอง"""
    db = get_db()
    inv = get_or_404("SELECT * FROM invoices WHERE id=?", (invoice_id,))
    description = request.form.get("description", "").strip()
    amount = to_float(request.form.get("amount"))
    penalty_types = penalty_type_list()
    if not billing.invoice_editable(inv):
        flash("บิลนี้ชำระครบหรือยกเลิกแล้ว แก้ไขรายการไม่ได้", "error")
    elif not description or amount == 0:
        flash("กรุณากรอกรายการและจำนวนเงิน", "error")
    elif billing.money(inv["total"] + amount) < inv["paid_amount"]:
        flash("ยอดบิลหลังแก้ไขจะน้อยกว่ายอดที่ชำระแล้ว", "error")
    else:
        if description in penalty_types:
            current = db.execute("SELECT COALESCE(SUM(amount),0) FROM invoice_items WHERE invoice_id=? AND"
                                 " kind='penalty' AND description=?", (invoice_id, description)).fetchone()[0]
            billing.set_penalty(db, invoice_id, description, current + amount)
        else:
            billing.add_manual_item(db, invoice_id, description, amount)
        log_activity(g.user, f"เพิ่ม '{description}' {amount:,.2f} บาท ในบิล {inv['invoice_no']}")
        db.commit()
        flash(f"เพิ่ม {description} แล้ว ยอดบิลถูกคำนวณใหม่", "success")
    return redirect(url_for("admin.invoice_detail", invoice_id=invoice_id))


@bp.route("/invoice-items/<int:item_id>/delete", methods=("POST",))
@admin_required
def invoice_item_delete(item_id):
    db = get_db()
    item = get_or_404("SELECT * FROM invoice_items WHERE id=?", (item_id,))
    inv = db.execute("SELECT * FROM invoices WHERE id=?", (item["invoice_id"],)).fetchone()
    if item["kind"] == "auto":
        flash("รายการที่ระบบคำนวณลบไม่ได้ (ให้ยกเลิกบิลแล้วออกใหม่แทน)", "error")
    elif not billing.invoice_editable(inv):
        flash("บิลนี้ชำระครบหรือยกเลิกแล้ว แก้ไขรายการไม่ได้", "error")
    elif billing.money(inv["total"] - item["amount"] - item["vat_amount"]) < inv["paid_amount"]:
        flash("ยอดบิลหลังลบรายการจะน้อยกว่ายอดที่ชำระแล้ว", "error")
    else:
        db.execute("DELETE FROM invoice_items WHERE id=?", (item_id,))
        # ถ้ารายการมาจาก "รายการเรียกเก็บเพิ่มเติม" ให้ลบต้นทางด้วย จะได้ไม่ถูกเรียกเก็บซ้ำในบิลถัดไป
        db.execute("DELETE FROM adhoc_charges WHERE invoice_id=? AND description=? AND amount=?",
                   (inv["id"], item["description"], item["amount"]))
        billing.recalc_invoice(db, inv["id"])
        log_activity(g.user, f"ลบรายการ '{item['description']}' จากบิล {inv['invoice_no']}")
        db.commit()
        flash("ลบรายการแล้ว ยอดบิลถูกคำนวณใหม่", "success")
    return redirect(url_for("admin.invoice_detail", invoice_id=item["invoice_id"]))


def penalty_type_list():
    return [x.strip() for x in get_settings().get("penalty_types", "").splitlines() if x.strip()]


@bp.route("/penalties", methods=("GET", "POST"))
@admin_required
def penalties():
    """กรอกเบี้ยปรับ (เช่น เบี้ยปรับ, เบี้ยปรับค่าน้ำ) ให้แต่ละห้องเองในงวดนั้น"""
    db = get_db()
    period = get_period_arg()
    types = penalty_type_list()
    invoices_ = db.execute("SELECT * FROM invoices WHERE period=? AND status!='void' ORDER BY unit_no",
                           (period,)).fetchall()
    if request.method == "POST":
        changed, errors = 0, []
        for inv in invoices_:
            if not billing.invoice_editable(inv):
                continue
            for idx, name in enumerate(types):
                raw = request.form.get(f"p_{inv['id']}_{idx}")
                if raw is None:
                    continue
                if billing.set_penalty(db, inv["id"], name, to_float(raw)):
                    changed += 1
            fresh = db.execute("SELECT total, paid_amount FROM invoices WHERE id=?", (inv["id"],)).fetchone()
            if fresh["total"] + 0.005 < fresh["paid_amount"]:
                errors.append(inv["unit_no"])
        if errors:
            db.rollback()
            flash("ยอดบิลหลังใส่เบี้ยปรับน้อยกว่ายอดที่ชำระแล้ว: ห้อง " + ", ".join(errors) + " (ยังไม่บันทึก)", "error")
        else:
            log_activity(g.user, f"บันทึกเบี้ยปรับงวด {period} ({changed} รายการ)")
            db.commit()
            flash(f"บันทึกเบี้ยปรับแล้ว ({changed} รายการที่เปลี่ยนแปลง) ยอดบิลถูกคำนวณใหม่", "success")
        return redirect(url_for("admin.penalties", period=period))

    current = {}
    for row in db.execute(
        "SELECT ii.invoice_id, ii.description, ii.amount FROM invoice_items ii JOIN invoices i ON i.id=ii.invoice_id"
        " WHERE i.period=? AND ii.kind='penalty'", (period,)):
        current[(row["invoice_id"], row["description"])] = row["amount"]
    return render_template("admin/penalties.html", period=period, types=types, invoices=invoices_, current=current)


@bp.route("/payments/<int:payment_id>/delete", methods=("POST",))
@admin_required
def payment_delete(payment_id):
    db = get_db()
    p = get_or_404("SELECT * FROM payments WHERE id=?", (payment_id,))
    db.execute("DELETE FROM payments WHERE id=?", (payment_id,))
    billing.refresh_invoice_status(db, p["invoice_id"])
    log_activity(g.user, f"ยกเลิกใบเสร็จ {p['receipt_no']}")
    db.commit()
    flash(f"ยกเลิกรายการรับชำระ {p['receipt_no']} แล้ว", "success")
    return redirect(url_for("admin.invoice_detail", invoice_id=p["invoice_id"]))


@bp.route("/payments/<int:payment_id>/receipt")
@admin_required
def receipt(payment_id):
    db = get_db()
    p = get_or_404("SELECT * FROM payments WHERE id=?", (payment_id,))
    inv, items, _payments, unit = load_invoice(p["invoice_id"])
    return render_template("receipt.html", p=p, inv=inv, items=items, unit=unit)


@bp.route("/invoices/<int:invoice_id>/void", methods=("POST",))
@admin_required
def invoice_void(invoice_id):
    db = get_db()
    inv = get_or_404("SELECT * FROM invoices WHERE id=?", (invoice_id,))
    if inv["paid_amount"] > 0:
        flash("บิลนี้มีการรับชำระแล้ว ต้องยกเลิกรายการรับชำระก่อน", "error")
    else:
        billing.void_invoice(db, invoice_id)
        log_activity(g.user, f"ยกเลิกใบแจ้งหนี้ {inv['invoice_no']}")
        db.commit()
        flash("ยกเลิกใบแจ้งหนี้แล้ว สามารถแก้ไขข้อมูลและออกบิลใหม่ได้ที่หน้า 'ออกใบแจ้งหนี้'", "success")
    return redirect(url_for("admin.invoice_detail", invoice_id=invoice_id))


@bp.route("/invoices/print")
@admin_required
def invoices_print():
    """พิมพ์ใบแจ้งหนี้ทั้งงวดในครั้งเดียว"""
    period = get_period_arg()
    db = get_db()
    ids = [r["id"] for r in db.execute(
        "SELECT id FROM invoices WHERE period=? AND status!='void' ORDER BY unit_no", (period,))]
    docs = [load_invoice(i) for i in ids]
    return render_template("invoice_batch.html", docs=docs, period=period)


# ---------------------------------------------------------------- reports
@bp.route("/reports")
@admin_required
def reports():
    db = get_db()
    year = request.args.get("year", type=int) or date.today().year
    by_month = db.execute(
        "SELECT period, COUNT(*) AS cnt, SUM(total) AS total, SUM(paid_amount) AS paid,"
        " SUM(total-paid_amount) AS outstanding FROM invoices WHERE status!='void' AND period LIKE ?"
        " GROUP BY period ORDER BY period", (f"{year}-%",)).fetchall()
    by_item = db.execute(
        "SELECT ii.description, SUM(ii.amount + ii.vat_amount) AS total, COUNT(*) AS cnt FROM invoice_items ii"
        " JOIN invoices i ON i.id=ii.invoice_id WHERE i.status!='void' AND i.period LIKE ?"
        " GROUP BY ii.description ORDER BY total DESC", (f"{year}-%",)).fetchall()
    collections = db.execute(
        "SELECT substr(paid_at,1,7) AS month, method, SUM(amount) AS total, COUNT(*) AS cnt FROM payments"
        " WHERE paid_at LIKE ? GROUP BY month, method ORDER BY month", (f"{year}-%",)).fetchall()
    outstanding = db.execute(
        "SELECT unit_id, unit_no, owner_name, COUNT(*) AS cnt, SUM(total-paid_amount) AS amount,"
        " MIN(period) AS oldest FROM invoices WHERE status IN ('unpaid','partial')"
        " GROUP BY unit_id ORDER BY amount DESC").fetchall()
    years = [r[0] for r in db.execute(
        "SELECT DISTINCT substr(period,1,4) FROM invoices ORDER BY 1 DESC")] or [str(year)]
    if str(year) not in years:
        years.insert(0, str(year))
    return render_template("admin/reports.html", year=year, years=years, by_month=by_month, by_item=by_item,
                           collections=collections, outstanding=outstanding)


def csv_response(filename, header, rows):
    buf = io.StringIO()
    buf.write("﻿")  # BOM ให้ Excel อ่านภาษาไทยได้
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@bp.route("/export/invoices.csv")
@admin_required
def export_invoices():
    db = get_db()
    year = request.args.get("year", "")
    sql, params = "SELECT * FROM invoices", []
    if year.isdigit():
        sql += " WHERE period LIKE ?"
        params.append(f"{year}-%")
    rows = db.execute(sql + " ORDER BY period, unit_no", params).fetchall()
    return csv_response(
        f"invoices{('-' + year) if year else ''}.csv",
        ["เลขที่", "งวด", "ห้อง", "เจ้าของ", "วันที่ออก", "ครบกำหนด", "ก่อนภาษี", "VAT", "ยอดรวม", "ชำระแล้ว",
         "คงค้าง", "สถานะ"],
        [[r["invoice_no"], r["period"], r["unit_no"], r["owner_name"], r["issue_date"], r["due_date"],
          r["subtotal"], r["vat"], r["total"], r["paid_amount"], round(r["total"] - r["paid_amount"], 2),
          billing.STATUS_LABELS[r["status"]]] for r in rows],
    )


@bp.route("/export/items.csv")
@admin_required
def export_items():
    db = get_db()
    year = request.args.get("year", "")
    sql = ("SELECT i.invoice_no, i.period, i.unit_no, i.status, ii.* FROM invoice_items ii"
           " JOIN invoices i ON i.id=ii.invoice_id")
    params = []
    if year.isdigit():
        sql += " WHERE i.period LIKE ?"
        params.append(f"{year}-%")
    rows = db.execute(sql + " ORDER BY i.period, i.unit_no, ii.sort_order", params).fetchall()
    return csv_response(
        f"invoice-items{('-' + year) if year else ''}.csv",
        ["เลขที่บิล", "งวด", "ห้อง", "สถานะ", "รายการ", "รายละเอียด", "จำนวน", "หน่วย", "ราคาต่อหน่วย", "จำนวนเงิน",
         "VAT"],
        [[r["invoice_no"], r["period"], r["unit_no"], billing.STATUS_LABELS[r["status"]], r["description"],
          r["detail"], r["quantity"], r["unit_label"], r["unit_price"], r["amount"], r["vat_amount"]] for r in rows],
    )


@bp.route("/export/payments.csv")
@admin_required
def export_payments():
    rows = get_db().execute(
        "SELECT p.*, i.invoice_no, i.unit_no, i.period FROM payments p JOIN invoices i ON i.id=p.invoice_id"
        " ORDER BY p.paid_at, p.id").fetchall()
    return csv_response(
        "payments.csv",
        ["เลขที่ใบเสร็จ", "วันที่ชำระ", "ห้อง", "เลขที่บิล", "งวด", "จำนวนเงิน", "ช่องทาง", "อ้างอิง", "หมายเหตุ"],
        [[r["receipt_no"], r["paid_at"], r["unit_no"], r["invoice_no"], r["period"], r["amount"], r["method"],
          r["reference"], r["note"]] for r in rows],
    )


# ---------------------------------------------------------------- settings / backup / log
@bp.route("/settings", methods=("GET", "POST"))
@admin_required
def settings_page():
    db = get_db()
    if request.method == "POST":
        for key in DEFAULT_SETTINGS:
            value = request.form.get(key, "").strip()
            if key in ("issue_day", "due_days"):
                value = str(max(int(to_float(value, 1)), 0))
            if key == "penalty_types":
                value = "\n".join(dict.fromkeys(x.strip() for x in value.splitlines() if x.strip()))
            db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, value))
        log_activity(g.user, "แก้ไขการตั้งค่าระบบ")
        db.commit()
        flash("บันทึกการตั้งค่าเรียบร้อย", "success")
        return redirect(url_for("admin.settings_page"))
    return render_template("admin/settings.html")


@bp.route("/backup")
@admin_required
def backup():
    """ดาวน์โหลดไฟล์ฐานข้อมูลทั้งหมด (สำรองข้อมูลย้อนหลัง)"""
    src = get_db()
    mem = sqlite3.connect(":memory:")
    src.backup(mem)
    data = mem.serialize()
    mem.close()
    log_activity(g.user, "ดาวน์โหลดไฟล์สำรองข้อมูล")
    src.commit()
    return send_file(io.BytesIO(data), mimetype="application/x-sqlite3", as_attachment=True,
                     download_name=f"condo-backup-{date.today().isoformat()}.sqlite3")


@bp.route("/activity")
@admin_required
def activity():
    rows = get_db().execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 500").fetchall()
    return render_template("admin/activity.html", rows=rows)


@bp.app_errorhandler(404)
def not_found(_e):
    return render_template("error.html", message="ไม่พบหน้าที่ต้องการ"), 404


@bp.app_errorhandler(400)
def bad_request(e):
    return render_template("error.html", message=getattr(e, "description", "คำขอไม่ถูกต้อง")), 400

