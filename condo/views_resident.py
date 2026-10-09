"""หน้าจอสำหรับลูกบ้าน: ดูใบแจ้งหนี้และประวัติของห้องตัวเอง"""
from datetime import date

from flask import Blueprint, abort, g, redirect, render_template, url_for

from .auth import login_required
from .db import get_db

bp = Blueprint("resident", __name__, url_prefix="/my")


@bp.route("/")
@login_required
def home():
    if g.user["role"] == "admin":
        return redirect(url_for("admin.dashboard"))
    db = get_db()
    unit = db.execute("SELECT * FROM units WHERE id=?", (g.user["unit_id"],)).fetchone()
    invoices = db.execute(
        "SELECT * FROM invoices WHERE unit_id=? AND status!='void' ORDER BY period DESC", (g.user["unit_id"],)
    ).fetchall()
    outstanding = sum(i["total"] - i["paid_amount"] for i in invoices if i["status"] in ("unpaid", "partial"))
    readings = db.execute(
        "SELECT m.*, c.name, c.unit_label FROM meter_readings m JOIN charge_types c ON c.id=m.charge_type_id"
        " WHERE m.unit_id=? ORDER BY m.period DESC, c.sort_order LIMIT 24", (g.user["unit_id"],)
    ).fetchall()
    return render_template("resident/home.html", unit=unit, invoices=invoices, outstanding=outstanding,
                           readings=readings)


@bp.route("/invoices/<int:invoice_id>")
@login_required
def invoice(invoice_id):
    db = get_db()
    inv = db.execute("SELECT * FROM invoices WHERE id=? AND status!='void'", (invoice_id,)).fetchone()
    if inv is None or (g.user["role"] != "admin" and inv["unit_id"] != g.user["unit_id"]):
        abort(404)
    items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY sort_order, id",
                       (invoice_id,)).fetchall()
    payments = db.execute("SELECT * FROM payments WHERE invoice_id=? ORDER BY paid_at", (invoice_id,)).fetchall()
    unit = db.execute("SELECT * FROM units WHERE id=?", (inv["unit_id"],)).fetchone()
    return render_template("invoice.html", inv=inv, items=items, payments=payments, unit=unit, admin=False,
                           today=date.today().isoformat())


@bp.route("/receipts/<int:payment_id>")
@login_required
def receipt(payment_id):
    db = get_db()
    p = db.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    if p is None:
        abort(404)
    inv = db.execute("SELECT * FROM invoices WHERE id=?", (p["invoice_id"],)).fetchone()
    if g.user["role"] != "admin" and inv["unit_id"] != g.user["unit_id"]:
        abort(404)
    items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY sort_order, id",
                       (inv["id"],)).fetchall()
    unit = db.execute("SELECT * FROM units WHERE id=?", (inv["unit_id"],)).fetchone()
    return render_template("receipt.html", p=p, inv=inv, items=items, unit=unit)
