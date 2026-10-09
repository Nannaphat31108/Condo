"""หน้าจอสำหรับผู้ดูแลระบบ (นิติบุคคล)"""
import base64
import calendar
import csv
import io
import os
import re
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
UNIT_FIELDS = ("unit_no", "unit_type", "building", "floor", "area_sqm", "owner_name", "phone", "tenant_name",
               "tenant_phone", "note")


def unit_form_data():
    data = {f: request.form.get(f, "").strip() for f in UNIT_FIELDS}
    data["area_sqm"] = to_float(data["area_sqm"])
    data["unit_type"] = data["unit_type"] if data["unit_type"] in billing.UNIT_TYPE_LABELS else "room"
    data["active"] = 1 if request.form.get("active") else 0
    return data


@bp.route("/units")
@admin_required
def units():
    q = request.args.get("q", "").strip()
    unit_type = request.args.get("type", "")
    sql = ("SELECT u.*, (SELECT COALESCE(SUM(total-paid_amount),0) FROM invoices i WHERE i.unit_id=u.id"
           " AND i.status IN ('unpaid','partial')) AS outstanding,"
           " (SELECT GROUP_CONCAT(username, ', ') FROM users WHERE unit_id=u.id) AS usernames FROM units u WHERE 1=1")
    params = []
    if unit_type in billing.UNIT_TYPE_LABELS:
        sql += " AND u.unit_type=?"
        params.append(unit_type)
    if q:
        sql += (" AND (u.unit_no LIKE ? OR u.owner_name LIKE ? OR u.phone LIKE ?"
                " OR u.tenant_name LIKE ? OR u.tenant_phone LIKE ?)")
        params += [f"%{q}%"] * 5
    rows = get_db().execute(sql + " ORDER BY u.active DESC, u.unit_type, length(u.unit_no), u.unit_no",
                            params).fetchall()
    counts = dict(get_db().execute("SELECT unit_type, COUNT(*) FROM units GROUP BY unit_type").fetchall())
    return render_template("admin/units.html", units=rows, q=q, unit_type=unit_type, counts=counts)


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
                        "UPDATE units SET unit_no=?, unit_type=?, building=?, floor=?, area_sqm=?, owner_name=?, phone=?,"
                        " tenant_name=?, tenant_phone=?, note=?, active=? WHERE id=?",
                        (*[data[f] for f in UNIT_FIELDS], data["active"], unit_id),
                    )
                    log_activity(g.user, f"แก้ไขห้อง {data['unit_no']}")
                else:
                    db.execute(
                        "INSERT INTO units (unit_no, unit_type, building, floor, area_sqm, owner_name, phone, tenant_name,"
                        " tenant_phone, note, active) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (*[data[f] for f in UNIT_FIELDS], data["active"]),
                    )
                    log_activity(g.user, f"เพิ่มห้อง {data['unit_no']}")
                db.commit()
                flash("บันทึกข้อมูลห้องเรียบร้อย", "success")
                return redirect(url_for("admin.units"))
            except sqlite3.IntegrityError:
                flash("เลขห้องนี้มีอยู่แล้ว", "error")
        unit = {**(dict(unit) if unit else {}), **data}
    if unit is None and request.args.get("type") == "shop":
        unit = {"unit_type": "shop", "active": 1}
    return render_template("admin/unit_form.html", unit=unit, unit_id=unit_id)


@bp.route("/units/import", methods=("GET", "POST"))
@admin_required
def unit_import():
    """นำเข้าห้องจำนวนมาก: สร้างตามชั้น/จำนวนห้อง หรือจาก CSV
    เลขห้อง,อาคาร,ชั้น,พื้นที่,ชื่อเจ้าของ,โทรเจ้าของ,ชื่อผู้เช่า,โทรผู้เช่า"""
    if request.method == "POST":
        db = get_db()
        added, skipped = 0, []
        if request.form.get("mode") == "sequence":
            # เลขห้องเรียงต่อกันทั้งตึก เช่น ชั้น 1 = 26/1-26/22, ชั้น 2 = 26/23-26/66
            prefix = request.form.get("prefix", "").strip()
            number = request.form.get("start_no", type=int) or 1
            floor = request.form.get("first_floor", type=int) or 1
            building = request.form.get("building", "").strip()
            area = to_float(request.form.get("area_sqm"))
            try:
                counts = [int(x) for x in re.split(r"[,\s]+", request.form.get("floor_counts", "").strip()) if x]
            except ValueError:
                counts = []
            if not counts or any(c < 1 or c > 500 for c in counts) or len(counts) > 100:
                flash("กรุณากรอกจำนวนห้องแต่ละชั้นเป็นตัวเลข คั่นด้วยจุลภาค เช่น 22,44,44,44,44", "error")
                return redirect(url_for("admin.unit_import"))
            for count in counts:
                for _ in range(count):
                    unit_no = f"{prefix}{number}"
                    try:
                        db.execute("INSERT INTO units (unit_no, building, floor, area_sqm) VALUES (?,?,?,?)",
                                   (unit_no, building, str(floor), area))
                        added += 1
                    except sqlite3.IntegrityError:
                        skipped.append(unit_no)
                    number += 1
                floor += 1
            log_activity(g.user, f"สร้างห้องเลขเรียงต่อกัน {added} ห้อง")
            db.commit()
            flash(f"สร้างห้องเรียบร้อย {added} ห้อง" + (f" (ข้ามเลขห้องที่มีอยู่แล้ว {len(skipped)} ห้อง)" if skipped else ""),
                  "success")
            return redirect(url_for("admin.units"))
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
            row = [c.strip() for c in row] + [""] * 8
            if not row[0] or row[0] in ("unit_no", "เลขห้อง"):
                continue
            try:
                db.execute(
                    "INSERT INTO units (unit_no, building, floor, area_sqm, owner_name, phone, tenant_name, tenant_phone)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (row[0], row[1], row[2], to_float(row[3]), row[4], row[5], row[6], row[7]),
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


def reset_unit_data(db, unit_id, clear_contacts=False):
    """ล้างข้อมูลการเงินของห้อง (บิล ใบเสร็จ มิเตอร์ รายการเพิ่มเติม) แต่เก็บห้องไว้"""
    db.execute("DELETE FROM payments WHERE invoice_id IN (SELECT id FROM invoices WHERE unit_id=?)", (unit_id,))
    db.execute("DELETE FROM adhoc_charges WHERE unit_id=?", (unit_id,))
    db.execute("DELETE FROM invoices WHERE unit_id=?", (unit_id,))
    db.execute("DELETE FROM meter_readings WHERE unit_id=?", (unit_id,))
    db.execute("DELETE FROM bill_exclusions WHERE unit_id=?", (unit_id,))
    if clear_contacts:
        db.execute("UPDATE units SET owner_name='', phone='', tenant_name='', tenant_phone='', note='' WHERE id=?",
                   (unit_id,))


@bp.route("/units/<int:unit_id>/reset", methods=("POST",))
@admin_required
def unit_reset(unit_id):
    db = get_db()
    unit = get_or_404("SELECT * FROM units WHERE id=?", (unit_id,))
    reset_unit_data(db, unit_id, bool(request.form.get("clear_contacts")))
    log_activity(g.user, f"รีเซ็ตข้อมูลห้อง {unit['unit_no']}")
    db.commit()
    flash(f"รีเซ็ตข้อมูลห้อง {unit['unit_no']} แล้ว (ลบบิล ใบเสร็จ เลขมิเตอร์ และรายการเพิ่มเติมทั้งหมด)", "success")
    return redirect(url_for("admin.unit_detail", unit_id=unit_id))


def natural_key(unit_no):
    return (len(unit_no), unit_no)


@bp.route("/units/bulk-delete", methods=("GET", "POST"))
@admin_required
def unit_bulk_delete():
    """ลบห้องหลายห้องตามช่วงเลขห้อง (เรียงแบบตัวเลข เช่น 101 ถึง 825) — ดูรายการก่อนยืนยัน"""
    db = get_db()
    first = request.values.get("from", "").strip()
    last = request.values.get("to", "").strip()
    units_ = []
    if first and last:
        lo, hi = natural_key(first), natural_key(last)
        units_ = [u for u in db.execute(
            "SELECT u.*, (SELECT COUNT(*) FROM invoices i WHERE i.unit_id=u.id) AS invoice_count,"
            " (SELECT COUNT(*) FROM payments p JOIN invoices i ON i.id=p.invoice_id WHERE i.unit_id=u.id) AS payment_count"
            " FROM units u ORDER BY length(u.unit_no), u.unit_no").fetchall()
            if lo <= natural_key(u["unit_no"]) <= hi]
    if request.method == "POST" and request.form.get("action") == "reset":
        clear_contacts = bool(request.form.get("clear_contacts"))
        for u in units_:
            reset_unit_data(db, u["id"], clear_contacts)
        log_activity(g.user, f"รีเซ็ตข้อมูล {len(units_)} ห้อง ({first} ถึง {last})")
        db.commit()
        flash(f"รีเซ็ตข้อมูลแล้ว {len(units_)} ห้อง (เก็บห้องไว้ ลบบิล ใบเสร็จ เลขมิเตอร์ และรายการเพิ่มเติม)", "success")
        return redirect(url_for("admin.units"))
    if request.method == "POST":
        include_history = bool(request.form.get("include_history"))
        deleted, kept = [], []
        for u in units_:
            if u["invoice_count"] and not include_history:
                kept.append(u["unit_no"])
                continue
            # ลบประวัติบิลของห้องนี้ (การชำระเงิน/รายการในบิลถูกลบตามอัตโนมัติ)
            db.execute("DELETE FROM payments WHERE invoice_id IN (SELECT id FROM invoices WHERE unit_id=?)", (u["id"],))
            db.execute("DELETE FROM invoices WHERE unit_id=?", (u["id"],))
            db.execute("DELETE FROM units WHERE id=?", (u["id"],))
            deleted.append(u["unit_no"])
        if deleted:
            log_activity(g.user, f"ลบห้อง {len(deleted)} ห้อง ({first} ถึง {last})"
                                 + (" พร้อมประวัติบิล" if include_history else ""))
        db.commit()
        flash(f"ลบห้องแล้ว {len(deleted)} ห้อง", "success")
        if kept:
            flash(f"ไม่ได้ลบ {len(kept)} ห้องที่มีใบแจ้งหนี้แล้ว (ติ๊ก 'ลบประวัติบิลด้วย' ถ้าเป็นข้อมูลทดลอง)", "error")
        return redirect(url_for("admin.units"))
    return render_template("admin/unit_bulk_delete.html", units=units_, first=first, last=last)


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
    units_list = db.execute("SELECT id, unit_no, owner_name, tenant_name FROM units WHERE active=1"
                            " ORDER BY length(unit_no), unit_no").fetchall()
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
    units_list = db.execute("SELECT * FROM units WHERE active=1 ORDER BY length(unit_no), unit_no").fetchall()
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
            "unit_type": f.get("unit_type") if f.get("unit_type") in billing.CHARGE_UNIT_TYPE_LABELS else "all",
            "manual_amount": 1 if f.get("manual_amount") else 0,
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
def save_reading(db, ct, unit, period, prev_raw, curr_raw, read_date, reset=False):
    """บันทึกเลขมิเตอร์ 1 รายการ คืน True = บันทึก, None = ไม่มีอะไรเปลี่ยน, ข้อความ = ผิดพลาด"""
    prev_raw, curr_raw = str(prev_raw or "").strip(), str(curr_raw or "").strip()
    if curr_raw == "":
        return None
    prev, curr = to_float(prev_raw), to_float(curr_raw)
    if curr < prev and not reset:
        return f"{unit['unit_no']} {ct['name']}: เลขครั้งนี้ ({curr_raw}) น้อยกว่าเลขครั้งก่อน ({prev_raw})"
    if curr < prev:  # มิเตอร์วนรอบ/เปลี่ยนมิเตอร์ใหม่: ถือว่าเริ่มนับจาก 0
        prev = 0
    invoiced = db.execute("SELECT invoice_no FROM invoices WHERE unit_id=? AND period=? AND status!='void'",
                          (unit["id"], period)).fetchone()
    existing = db.execute("SELECT * FROM meter_readings WHERE charge_type_id=? AND unit_id=? AND period=?",
                          (ct["id"], unit["id"], period)).fetchone()
    if existing and (existing["prev_reading"], existing["curr_reading"]) == (prev, curr):
        return None
    if invoiced:
        return (f"{unit['unit_no']} {ct['name']}: ออกบิล {invoiced['invoice_no']} ไปแล้ว"
                " ต้องยกเลิกบิลก่อนจึงแก้เลขมิเตอร์ได้")
    db.execute(
        "INSERT INTO meter_readings (charge_type_id, unit_id, period, prev_reading, curr_reading, read_date)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT (charge_type_id, unit_id, period) DO UPDATE SET"
        " prev_reading=excluded.prev_reading, curr_reading=excluded.curr_reading,"
        " read_date=excluded.read_date, recorded_at=datetime('now','localtime')",
        (ct["id"], unit["id"], period, prev, curr, read_date),
    )
    return True


def last_readings(db, ct_id, period):
    """เลขมิเตอร์ล่าสุดก่อนงวดนี้ของทุกห้อง {unit_id: curr_reading}"""
    return {r["unit_id"]: r["curr_reading"] for r in db.execute(
        "SELECT m.unit_id, m.curr_reading FROM meter_readings m WHERE m.charge_type_id=? AND m.period=("
        " SELECT MAX(period) FROM meter_readings x WHERE x.charge_type_id=m.charge_type_id"
        " AND x.unit_id=m.unit_id AND x.period<?)", (ct_id, period))}


def units_for_charge(db, ct):
    sql = "SELECT * FROM units WHERE active=1"
    params = []
    if ct["unit_type"] in ("room", "shop"):
        sql += " AND unit_type=?"
        params.append(ct["unit_type"])
    return db.execute(sql + " ORDER BY length(unit_no), unit_no", params).fetchall()

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
    units_list = units_for_charge(db, ct)

    if request.method == "POST":
        errors, saved = [], 0
        read_date = request.form.get("read_date") or date.today().isoformat()
        for unit in units_list:
            result = save_reading(db, ct, unit, period, request.form.get(f"prev_{unit['id']}", ""),
                                  request.form.get(f"curr_{unit['id']}", ""), read_date,
                                  bool(request.form.get(f"reset_{unit['id']}")))
            if result is True:
                saved += 1
            elif result:
                errors.append(result)
        log_activity(g.user, f"บันทึกมิเตอร์ {ct['name']} งวด {period} จำนวน {saved} ห้อง")
        db.commit()
        for e in errors:
            flash(e, "error")
        flash(f"บันทึกเลขมิเตอร์ {saved} ห้อง", "success")
        return redirect(url_for("admin.meters", period=period, charge_type_id=ct["id"]))

    current = {r["unit_id"]: r for r in db.execute(
        "SELECT * FROM meter_readings WHERE charge_type_id=? AND period=?", (ct["id"], period))}
    # เลขครั้งก่อน = เลขปัจจุบันของงวดล่าสุดก่อนหน้า
    last = last_readings(db, ct["id"], period)
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
    read_date = next((r["read_date"] for r in current.values() if r["read_date"]), date.today().isoformat())
    return render_template("admin/meters.html", meter_types=meter_types, ct=ct, period=period, rows=rows,
                           read_date=read_date)


# ---------------------------------------------------------------- adhoc charges
@bp.route("/adhoc", methods=("GET", "POST"))
@admin_required
def adhoc():
    db = get_db()
    period = get_period_arg()
    units_list = db.execute("SELECT * FROM units WHERE active=1 ORDER BY length(unit_no), unit_no").fetchall()
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


# ---------------------------------------------------------------- กรอกรวมทุกรายการ (หน้าเดียว)
def sheet_columns(db, unit_type):
    charge_types = db.execute(
        "SELECT * FROM charge_types WHERE active=1 AND unit_type IN ('all', ?) ORDER BY sort_order, id", (unit_type,)
    ).fetchall()
    cols = []
    for ct in charge_types:
        if ct["method"] in billing.METER_METHODS:
            kind = "meter"
        elif ct["manual_amount"]:
            kind = "manual"
        else:
            kind = "auto"
        cols.append({"ct": ct, "kind": kind})
    return cols


def set_exclusion(db, unit_id, period, charge_type_id, excluded):
    """ติ๊กออก = ไม่เรียกเก็บค่าบริการนี้จากห้องนี้ในงวดนี้"""
    if excluded:
        db.execute("INSERT OR IGNORE INTO bill_exclusions (unit_id, period, charge_type_id) VALUES (?,?,?)",
                   (unit_id, period, charge_type_id))
    else:
        db.execute("DELETE FROM bill_exclusions WHERE unit_id=? AND period=? AND charge_type_id=?",
                   (unit_id, period, charge_type_id))


def upsert_adhoc(db, unit_id, period, kind, description, amount):
    """รายการที่ยังไม่ออกบิล (เบี้ยปรับ / อื่น ๆ) จากหน้ากรอกรวม: 0 หรือว่าง = ลบ"""
    if kind == "penalty":
        row = db.execute("SELECT id FROM adhoc_charges WHERE unit_id=? AND period=? AND kind='penalty' AND description=?"
                         " AND invoice_id IS NULL", (unit_id, period, description)).fetchone()
    else:
        row = db.execute("SELECT id FROM adhoc_charges WHERE unit_id=? AND period=? AND kind=? AND invoice_id IS NULL",
                         (unit_id, period, kind)).fetchone()
    if not amount:
        if row:
            db.execute("DELETE FROM adhoc_charges WHERE id=?", (row["id"],))
    elif row:
        db.execute("UPDATE adhoc_charges SET amount=?, description=? WHERE id=?", (amount, description, row["id"]))
    else:
        db.execute("INSERT INTO adhoc_charges (unit_id, period, description, amount, kind) VALUES (?,?,?,?,?)",
                   (unit_id, period, description, amount, kind))


@bp.route("/sheet", methods=("GET", "POST"))
@admin_required
def sheet():
    db = get_db()
    period = get_period_arg()
    unit_type = request.values.get("type") if request.values.get("type") in billing.UNIT_TYPE_LABELS else "room"
    cols = sheet_columns(db, unit_type)
    penalty_types = penalty_type_list()
    units_ = db.execute("SELECT * FROM units WHERE active=1 AND unit_type=? ORDER BY length(unit_no), unit_no",
                        (unit_type,)).fetchall()
    invoices_ = {r["unit_id"]: r for r in db.execute(
        "SELECT * FROM invoices WHERE period=? AND status!='void'", (period,))}
    selections = billing.load_selections(db)

    if request.method == "POST":
        f = request.form
        read_date = f.get("read_date") or date.today().isoformat()
        errors, changed = [], 0
        for unit in units_:
            uid = unit["id"]
            inv = invoices_.get(uid)
            if not f.get(f"row_{uid}"):
                continue  # แถวที่แก้ไขไม่ได้ (บิลชำระครบแล้ว)
            if inv:
                # ออกบิลแล้ว (ยังไม่ชำระครบ): ติ๊กเพิ่ม/เอาออกรายการ และแก้เบี้ยปรับ
                db.execute("SAVEPOINT sheet_row")
                have = {it["charge_type_id"]: it for it in db.execute(
                    "SELECT * FROM invoice_items WHERE invoice_id=? AND charge_type_id IS NOT NULL", (inv["id"],))}
                for col in cols:
                    ct = col["ct"]
                    want = bool(f.get(f"inc_{ct['id']}_{uid}"))
                    if want and ct["id"] not in have:
                        item = billing.charge_item(db, ct, unit, period, selections)
                        if item is None:
                            errors.append(f"{unit['unit_no']} {ct['name']}: ยังไม่ได้จดมิเตอร์ จึงเพิ่มในบิลไม่ได้")
                            continue
                        billing.insert_item(db, inv["id"], item)
                        set_exclusion(db, uid, period, ct["id"], False)
                        changed += 1
                    elif not want and ct["id"] in have:
                        db.execute("DELETE FROM invoice_items WHERE id=?", (have[ct["id"]]["id"],))
                        set_exclusion(db, uid, period, ct["id"], True)
                        changed += 1
                for idx, name in enumerate(penalty_types):
                    raw = f.get(f"pen_{idx}_{uid}")
                    if raw is not None and billing.set_penalty(db, inv["id"], name, to_float(raw)):
                        changed += 1
                billing.renumber_items(db, inv["id"])
                billing.recalc_invoice(db, inv["id"])
                fresh = db.execute("SELECT total, paid_amount FROM invoices WHERE id=?", (inv["id"],)).fetchone()
                if fresh["total"] + 0.005 < fresh["paid_amount"]:
                    db.execute("ROLLBACK TO sheet_row")
                    errors.append(f"{unit['unit_no']}: ยอดบิลหลังแก้จะน้อยกว่าที่ชำระแล้ว ({fresh['paid_amount']:,.2f}) ยังไม่บันทึก")
                db.execute("RELEASE sheet_row")
                continue
            for col in cols:
                ct = col["ct"]
                if f.get(f"has_{ct['id']}_{uid}"):
                    set_exclusion(db, uid, period, ct["id"], not f.get(f"inc_{ct['id']}_{uid}"))
            for col in cols:
                ct = col["ct"]
                if col["kind"] == "meter":
                    result = save_reading(db, ct, unit, period, f.get(f"prev_{ct['id']}_{uid}"),
                                          f.get(f"curr_{ct['id']}_{uid}"), read_date)
                    if result is True:
                        changed += 1
                    elif result:
                        errors.append(result)
                elif col["kind"] == "manual":
                    raw = f.get(f"amt_{ct['id']}_{uid}")
                    if raw is None:
                        continue
                    amount = billing.money(to_float(raw))
                    current = selections.get(ct["id"], {}).get(uid)
                    if (current or 0) == amount:
                        continue
                    if amount:
                        db.execute("INSERT INTO charge_type_units (charge_type_id, unit_id, amount_override) VALUES (?,?,?)"
                                   " ON CONFLICT (charge_type_id, unit_id) DO UPDATE SET amount_override=excluded.amount_override",
                                   (ct["id"], uid, amount))
                    else:
                        db.execute("DELETE FROM charge_type_units WHERE charge_type_id=? AND unit_id=?", (ct["id"], uid))
                    changed += 1
            for idx, name in enumerate(penalty_types):
                raw = f.get(f"pen_{idx}_{uid}")
                if raw is not None:
                    upsert_adhoc(db, uid, period, "penalty", name, billing.money(to_float(raw)))
            raw = f.get(f"oth_{uid}")
            if raw is not None:
                upsert_adhoc(db, uid, period, "other", f.get(f"othd_{uid}", "").strip() or "อื่น ๆ",
                             billing.money(to_float(raw)))
        log_activity(g.user, f"กรอกรวม {billing.UNIT_TYPE_LABELS[unit_type]} งวด {period}")
        db.commit()
        for e in errors[:20]:
            flash(e, "error")
        if f.get("action") == "bill":
            result = billing.generate_invoices(db, period, get_settings(), unit_ids=[u["id"] for u in units_])
            log_activity(g.user, f"ออกใบแจ้งหนี้งวด {period} จำนวน {len(result['created'])} ฉบับ (จากหน้ากรอกรวม)")
            db.commit()
            flash(f"บันทึกแล้ว และออกใบแจ้งหนี้ใหม่ {len(result['created'])} ฉบับ", "success")
            if result["missing_meter"]:
                flash(f"ยังไม่ออกบิล {len(result['missing_meter'])} รายการ เพราะยังไม่จดมิเตอร์: "
                      + ", ".join(result["missing_meter"][:30]), "error")
        else:
            flash("บันทึกข้อมูลเรียบร้อย", "success")
        return redirect(url_for("admin.sheet", period=period, type=unit_type))

    # ---- เตรียมข้อมูลแสดงผล
    readings = {}
    for col in cols:
        if col["kind"] == "meter":
            ct_id = col["ct"]["id"]
            readings[ct_id] = {
                "current": {r["unit_id"]: r for r in db.execute(
                    "SELECT * FROM meter_readings WHERE charge_type_id=? AND period=?", (ct_id, period))},
                "last": last_readings(db, ct_id, period),
            }
    pending = {}
    for a in db.execute("SELECT * FROM adhoc_charges WHERE period=? AND invoice_id IS NULL", (period,)):
        key = (a["unit_id"], a["description"]) if a["kind"] == "penalty" else (a["unit_id"], a["kind"])
        pending[key] = a
    all_cts = db.execute("SELECT * FROM charge_types ORDER BY sort_order, id").fetchall()
    exclusions = {(r["unit_id"], r["charge_type_id"]) for r in db.execute(
        "SELECT * FROM bill_exclusions WHERE period=?", (period,))}
    rows = []
    for unit in units_:
        uid = unit["id"]
        inv = invoices_.get(uid)
        editable = inv is None or billing.invoice_editable(inv)
        row = {"unit": unit, "inv": inv, "editable": editable, "cells": {}, "penalties": {}, "other": None}
        by_ct = {}
        if inv:
            items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (inv["id"],)).fetchall()
            for it in items:
                if it["charge_type_id"]:
                    by_ct[it["charge_type_id"]] = it
                elif it["kind"] == "penalty":
                    row["penalties"][it["description"]] = it["amount"]
            other = sum(it["amount"] for it in items if not it["charge_type_id"] and it["kind"] != "penalty")
            row["other"] = {"amount": other}
            row["total"] = inv["total"]
        for col in cols:
            ct = col["ct"]
            sel = selections.get(ct["id"], {})
            applies = col["kind"] == "manual" or billing.charge_applies(ct, period, unit, sel)
            cell = {"applies": applies, "included": (uid, ct["id"]) not in exclusions, "amount": None}
            if inv:
                item = by_ct.get(ct["id"])
                cell.update(item=item, included=item is not None, applies=applies or item is not None)
                if item:
                    cell["amount"] = item["amount"] + item["vat_amount"]
                elif applies and col["kind"] != "manual":
                    est = billing.charge_item(db, ct, unit, period, selections)  # ยอดถ้าติ๊กเพิ่มกลับ
                    cell["amount"] = est["amount"] + est["vat_amount"] if est else None
            elif col["kind"] == "meter":
                r = readings[ct["id"]]
                cur = r["current"].get(uid)
                cell.update(prev=cur["prev_reading"] if cur else r["last"].get(uid, 0),
                            curr=cur["curr_reading"] if cur else None)
            elif col["kind"] == "manual":
                cell["amount"] = sel.get(uid)
            elif applies:
                item = billing.compute_item(ct, unit, None, sel.get(uid))
                cell["amount"] = item["amount"] + item["vat_amount"]
            row["cells"][ct["id"]] = cell
        if not inv:
            for name in penalty_types:
                a = pending.get((uid, name))
                row["penalties"][name] = a["amount"] if a else None
            a = pending.get((uid, "other"))
            row["other"] = {"amount": a["amount"] if a else None, "description": a["description"] if a else ""}
            items, missing, _ = billing.build_unit_items(db, unit, period, all_cts, selections)
            row["total"] = billing.money(sum(i["amount"] + i["vat_amount"] for i in items))
            row["missing"] = missing
        rows.append(row)
    counts = dict(db.execute("SELECT unit_type, COUNT(*) FROM units WHERE active=1 GROUP BY unit_type").fetchall())
    read_date = next((r["read_date"] for m in readings.values() for r in m["current"].values() if r["read_date"]),
                     date.today().isoformat())
    return render_template("admin/sheet.html", period=period, unit_type=unit_type, cols=cols, rows=rows,
                           penalty_types=penalty_types, counts=counts, read_date=read_date)


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
        meter_status.append({"ct": m, "done": done, "total": len(units_for_charge(db, m))})
    invoices = db.execute("SELECT * FROM invoices WHERE period=? ORDER BY length(unit_no), unit_no", (period,)).fetchall()
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
        sql += " AND (unit_no LIKE ? OR owner_name LIKE ? OR tenant_name LIKE ? OR invoice_no LIKE ?)"
        params += [f"%{q}%"] * 4
    rows = db.execute(sql + " ORDER BY period DESC, length(unit_no), unit_no LIMIT 1000", params).fetchall()
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
        "SELECT * FROM invoices WHERE period=? AND status IN ('unpaid','partial') ORDER BY length(unit_no), unit_no", (period,)
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
        if receipts and request.form.get("print_receipt"):
            ids = [r[0] for r in db.execute(
                f"SELECT id FROM payments WHERE receipt_no IN ({','.join('?' * len(receipts))})", receipts)]
            return redirect(url_for("admin.receipts_print", ids=",".join(map(str, ids)), print=1, period=period))
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
    ids = [int(x) for x in request.args.get("ids", "").split(",") if x.isdigit()]
    if ids:  # ใบเสร็จที่เพิ่งรับชำระ
        rows = db.execute(f"SELECT p.* FROM payments p JOIN invoices i ON i.id=p.invoice_id"
                          f" WHERE p.id IN ({','.join('?' * len(ids))}) ORDER BY length(i.unit_no), i.unit_no, p.id",
                          ids).fetchall()
    elif by == "paid":
        rows = db.execute("SELECT p.* FROM payments p WHERE substr(p.paid_at,1,7)=? ORDER BY p.paid_at, p.id",
                          (period,)).fetchall()
    else:
        rows = db.execute("SELECT p.* FROM payments p JOIN invoices i ON i.id=p.invoice_id WHERE i.period=?"
                          " ORDER BY length(i.unit_no), i.unit_no, p.id", (period,)).fetchall()
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
        if request.form.get("print_receipt"):
            payment_id = db.execute("SELECT id FROM payments WHERE receipt_no=?", (receipt_no,)).fetchone()[0]
            return redirect(url_for("admin.receipt", payment_id=payment_id, print=1))
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
    invoices_ = db.execute("SELECT * FROM invoices WHERE period=? AND status!='void' ORDER BY length(unit_no), unit_no",
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
        "SELECT id FROM invoices WHERE period=? AND status!='void' ORDER BY length(unit_no), unit_no", (period,))]
    docs = [load_invoice(i) for i in ids]
    return render_template("invoice_batch.html", docs=docs, period=period)


# ---------------------------------------------------------------- reports
def collections_between(db, start, end):
    """รายการรับชำระระหว่างวันที่ start ถึง end (รวม) พร้อมแยกยอดตามรายการในบิล

    ใบเสร็จหนึ่งใบอาจจ่ายหลายรายการ: แบ่งยอดที่รับตามสัดส่วนของรายการในบิล
    (จ่ายครบ = ได้ยอดตรงตามรายการ) และปัดเศษให้ผลรวมตรงกับยอดที่รับจริง
    """
    payments = db.execute(
        "SELECT p.*, i.invoice_no, i.unit_no, i.owner_name, i.tenant_name, i.period, i.total AS invoice_total"
        " FROM payments p JOIN invoices i ON i.id=p.invoice_id WHERE p.paid_at BETWEEN ? AND ?"
        " ORDER BY p.paid_at, p.id", (start, end)).fetchall()
    items_cache, rows = {}, []
    for p in payments:
        if p["invoice_id"] not in items_cache:
            items_cache[p["invoice_id"]] = db.execute(
                "SELECT description, amount + vat_amount AS amount FROM invoice_items WHERE invoice_id=?"
                " ORDER BY sort_order, id", (p["invoice_id"],)).fetchall()
        items = items_cache[p["invoice_id"]]
        ratio = p["amount"] / p["invoice_total"] if p["invoice_total"] else 0
        parts = {}
        for it in items:
            parts[it["description"]] = parts.get(it["description"], 0) + billing.money(it["amount"] * ratio)
        diff = billing.money(p["amount"] - sum(parts.values()))
        if parts and diff:
            biggest = max(parts, key=lambda k: parts[k])
            parts[biggest] = billing.money(parts[biggest] + diff)
        rows.append({"p": p, "parts": parts})
    return rows


def item_columns(rows):
    """ชื่อรายการเรียงตามลำดับค่าบริการ แล้วตามด้วยรายการอื่น"""
    order = {r["name"]: (r["sort_order"], r["id"]) for r in get_db().execute("SELECT * FROM charge_types")}
    names = {k for r in rows for k in r["parts"]}
    return sorted(names, key=lambda n: (0, *order[n]) if n in order else (1, 0, n))


@bp.route("/reports/daily")
@admin_required
def report_daily():
    db = get_db()
    day = request.args.get("date", "")
    try:
        day = date.fromisoformat(day).isoformat()
    except ValueError:
        day = date.today().isoformat()
    month = request.args.get("month", "") if valid_period(request.args.get("month", "")) else day[:7]
    # รายละเอียดของวันที่เลือก
    rows = collections_between(db, day, day)
    by_item, by_method = {}, {}
    for r in rows:
        for k, v in r["parts"].items():
            by_item[k] = billing.money(by_item.get(k, 0) + v)
        m = r["p"]["method"] or "-"
        cnt, amt = by_method.get(m, (0, 0))
        by_method[m] = (cnt + 1, billing.money(amt + r["p"]["amount"]))
    issued = db.execute("SELECT COUNT(*) AS cnt, COALESCE(SUM(total),0) AS total FROM invoices"
                        " WHERE issue_date=? AND status!='void'", (day,)).fetchone()
    # ตารางรายวันทั้งเดือน
    y, mo = (int(x) for x in month.split("-"))
    last_day = calendar.monthrange(y, mo)[1]
    month_rows = collections_between(db, f"{month}-01", f"{month}-{last_day:02d}")
    cols = item_columns(month_rows)
    days = {}
    for r in month_rows:
        d = days.setdefault(r["p"]["paid_at"][:10], {"count": 0, "total": 0.0, "parts": {}})
        d["count"] += 1
        d["total"] = billing.money(d["total"] + r["p"]["amount"])
        for k, v in r["parts"].items():
            d["parts"][k] = billing.money(d["parts"].get(k, 0) + v)
    month_totals = {c: billing.money(sum(d["parts"].get(c, 0) for d in days.values())) for c in cols}
    if request.args.get("export") == "csv":
        return csv_response(
            f"daily-{month}.csv", ["วันที่", "จำนวนใบเสร็จ", *cols, "รวม"],
            [[d, v["count"], *[v["parts"].get(c, 0) for c in cols], v["total"]] for d, v in sorted(days.items())],
        )
    return render_template("admin/report_daily.html", day=day, month=month, rows=rows, by_item=by_item,
                           item_cols=item_columns(rows), by_method=by_method, issued=issued, cols=cols,
                           days=sorted(days.items()), month_totals=month_totals,
                           month_total=billing.money(sum(d["total"] for d in days.values())),
                           month_count=sum(d["count"] for d in days.values()))


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
    rows = db.execute(sql + " ORDER BY period, length(unit_no), unit_no", params).fetchall()
    return csv_response(
        f"invoices{('-' + year) if year else ''}.csv",
        ["เลขที่", "งวด", "ห้อง", "เจ้าของ", "ผู้เช่า", "วันที่ออก", "ครบกำหนด", "ก่อนภาษี", "VAT", "ยอดรวม", "ชำระแล้ว",
         "คงค้าง", "สถานะ"],
        [[r["invoice_no"], r["period"], r["unit_no"], r["owner_name"], r["tenant_name"], r["issue_date"], r["due_date"],
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
    rows = db.execute(sql + " ORDER BY i.period, length(i.unit_no), i.unit_no, ii.sort_order", params).fetchall()
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
        upload = request.files.get("logo_file")
        logo = get_settings().get("logo", "")
        if request.form.get("logo_reset"):
            logo = ""
        elif upload and upload.filename:
            data = upload.read()
            if upload.mimetype not in ("image/png", "image/jpeg", "image/gif", "image/webp") or len(data) > 1_000_000:
                flash("โลโก้ต้องเป็นไฟล์รูป PNG/JPG ขนาดไม่เกิน 1 MB", "error")
                return redirect(url_for("admin.settings_page"))
            logo = f"data:{upload.mimetype};base64,{base64.b64encode(data).decode()}"
        for key in DEFAULT_SETTINGS:
            value = logo if key == "logo" else request.form.get(key, "").strip()
            if key in ("issue_day", "due_days"):
                value = str(max(int(to_float(value, 1)), 0))
            if key == "print_mode" and value not in ("color", "eco"):
                value = "color"
            if key == "auto_print_receipt":
                value = "1" if value else "0"
            if key == "penalty_types":
                value = "\n".join(dict.fromkeys(x.strip() for x in value.splitlines() if x.strip()))
            db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, value))
        log_activity(g.user, "แก้ไขการตั้งค่าระบบ")
        db.commit()
        flash("บันทึกการตั้งค่าเรียบร้อย", "success")
        return redirect(url_for("admin.settings_page"))
    return render_template("admin/settings.html")


def site_url():
    url = request.url_root
    if os.environ.get("RENDER") and url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url


@bp.route("/printer")
@admin_required
def printer():
    return render_template("admin/printer.html", site=site_url())


@bp.route("/printer/test")
@admin_required
def printer_test():
    return render_template("printer_test.html")


@bp.route("/printer/shortcut.bat")
@admin_required
def printer_shortcut():
    """ไฟล์เปิดระบบบน Windows ด้วย Chrome/Edge โหมดพิมพ์ทันที (ไม่ถามหน้าต่างพิมพ์ ส่งเข้าเครื่องพิมพ์หลักเลย)"""
    lines = [
        "@echo off",
        "REM Condo: open the system with direct printing to the default printer (Epson L3350)",
        f'set "URL={site_url()}"',
        r'set "BROWSER=%ProgramFiles%\Google\Chrome\Application\chrome.exe"',
        r'if not exist "%BROWSER%" set "BROWSER=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"',
        r'if not exist "%BROWSER%" set "BROWSER=%LocalAppData%\Google\Chrome\Application\chrome.exe"',
        r'if not exist "%BROWSER%" set "BROWSER=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"',
        r'if not exist "%BROWSER%" set "BROWSER=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"',
        r'if not exist "%BROWSER%" (echo Chrome or Edge not found & pause & exit /b 1)',
        r'start "" "%BROWSER%" --kiosk-printing --user-data-dir="%LocalAppData%\CondoPrint" "%URL%"',
    ]
    return Response("\r\n".join(lines) + "\r\n", mimetype="application/octet-stream",
                    headers={"Content-Disposition": "attachment; filename=condo-print.bat"})


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

