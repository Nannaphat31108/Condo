"""ประนอมหนี้: ห้องที่ค้างชำระหลายเดือนและจ่ายครั้งเดียวไม่ไหว ตกลงผ่อนเป็นงวด ๆ

แผนผ่อนผูกกับบิลค้างของห้อง (และยอดค้างยกมาที่ยังไม่มีบิล ระบบออกบิลให้) เงินที่ผ่อนแต่ละครั้ง
ตัดชำระบิลเก่าที่สุดก่อน ออกใบเสร็จตามปกติ รายงานรายวัน/รายเดือนจึงนับรายรับให้เอง
"""
import math
from datetime import date, timedelta

from flask import flash, g, redirect, render_template, request, url_for

from . import billing
from .auth import admin_required
from .db import get_db, get_settings, log_activity
from .views_admin import PAYMENT_METHODS, bp, get_or_404, record_payment, to_float, valid_period

STATUS = {"active": "กำลังผ่อน", "done": "ผ่อนครบแล้ว", "cancelled": "ยกเลิก"}


def outstanding_invoices(db, unit_id):
    return db.execute("SELECT * FROM invoices WHERE unit_id=? AND status IN ('unpaid','partial')"
                      " ORDER BY period, id", (unit_id,)).fetchall()


def plan_invoices(db, plan_id):
    return db.execute("SELECT i.* FROM invoices i JOIN debt_plan_invoices d ON d.invoice_id=i.id"
                      " WHERE d.plan_id=? ORDER BY i.period, i.id", (plan_id,)).fetchall()


def plan_state(db, plan):
    """ยอดผ่อนแล้ว คงเหลือ และตารางงวด (ชำระแล้ว / ถึงกำหนด / เกินกำหนด)"""
    invs = plan_invoices(db, plan["id"])
    live = [i for i in invs if i["status"] != "void"]
    paid = billing.money(max(sum(i["paid_amount"] for i in live) - plan["paid_base"], 0))
    total = plan["total"]
    current = billing.current_period()
    schedule, cum = [], 0.0
    for k in range(1, plan["installments"] + 1):
        amount = billing.money(min(plan["monthly"], total - cum)) if k < plan["installments"] else billing.money(total - cum)
        cum = billing.money(cum + amount)
        due = billing.add_months(plan["start_period"], k - 1)
        if paid + 0.005 >= cum:
            state = "paid"
        elif paid > cum - amount + 0.005:
            state = "partial"
        else:
            state = "overdue" if due < current else "due" if due == current else "upcoming"
        schedule.append({"no": k, "period": due, "amount": amount, "cum": cum, "state": state})
    remaining = billing.money(max(total - paid, 0))
    next_due = next((s for s in schedule if s["state"] != "paid"), None)
    overdue = sum(1 for s in schedule if s["state"] != "paid" and s["period"] < current)
    return {"invoices": invs, "paid": paid, "remaining": remaining, "schedule": schedule, "next": next_due,
            "overdue": overdue}


@bp.route("/debts")
@admin_required
def debts():
    db = get_db()
    status = request.args.get("status") if request.args.get("status") in STATUS else "active"
    plans = []
    for p in db.execute("SELECT d.*, u.unit_no, u.owner_name, u.tenant_name FROM debt_plans d"
                        " JOIN units u ON u.id=d.unit_id WHERE d.status=? ORDER BY length(u.unit_no), u.unit_no",
                        (status,)):
        plans.append({"plan": p, **plan_state(db, p)})
    # ห้องที่ค้างชำระ (ยังไม่อยู่ในแผนที่กำลังผ่อน) เรียงยอดค้างมากไปน้อย
    debtors = db.execute(
        "SELECT u.id, u.unit_no, u.owner_name, u.tenant_name, COUNT(i.id) AS cnt, SUM(i.total - i.paid_amount) AS due,"
        " MIN(i.period) AS since FROM invoices i JOIN units u ON u.id=i.unit_id"
        " WHERE i.status IN ('unpaid','partial') AND i.id NOT IN (SELECT dpi.invoice_id FROM debt_plan_invoices dpi"
        " JOIN debt_plans dp ON dp.id=dpi.plan_id WHERE dp.status='active')"
        " GROUP BY u.id ORDER BY due DESC").fetchall()
    units = db.execute("SELECT id, unit_no, owner_name FROM units WHERE active=1 ORDER BY unit_type, length(unit_no),"
                       " unit_no").fetchall()
    return render_template("admin/debts.html", plans=plans, debtors=debtors, units=units, status=status,
                           statuses=STATUS)


@bp.route("/debts/new", methods=("GET", "POST"))
@admin_required
def debt_new():
    db = get_db()
    unit = get_or_404("SELECT * FROM units WHERE id=?", (request.values.get("unit_id", type=int),))
    in_plan = {r[0] for r in db.execute(
        "SELECT dpi.invoice_id FROM debt_plan_invoices dpi JOIN debt_plans dp ON dp.id=dpi.plan_id"
        " WHERE dp.status='active'")}
    invs = [i for i in outstanding_invoices(db, unit["id"]) if i["id"] not in in_plan]
    if request.method == "POST":
        f = request.form
        chosen = [i for i in invs if f.get(f"inv_{i['id']}")]
        lump = billing.money(to_float(f.get("lump_amount")))
        discount = billing.money(to_float(f.get("discount")))
        installments = max(int(to_float(f.get("installments"), 0)), 0)
        start = f.get("start_period") if valid_period(f.get("start_period")) else billing.current_period()
        agreement_date = f.get("agreement_date") or date.today().isoformat()
        total = billing.money(sum(i["total"] - i["paid_amount"] for i in chosen) + lump)
        error = None
        if total <= 0:
            error = "ยังไม่ได้เลือกบิลค้าง หรือกรอกยอดค้างยกมา"
        elif not 1 <= installments <= 240:
            error = "จำนวนงวดต้องอยู่ระหว่าง 1–240 งวด"
        elif discount < 0 or discount >= total:
            error = "ส่วนลดต้องน้อยกว่ายอดหนี้"
        if error:
            flash(error, "error")
            return render_template("admin/debt_form.html", unit=unit, invs=invs, form=f, today=date.today().isoformat(),
                                   current_period=billing.current_period())
        settings = get_settings()
        today = date.today().isoformat()
        if lump:
            due_date = (date.today() + timedelta(days=int(settings.get("due_days") or 15))).isoformat()
            item = {"charge_type_id": None, "description": f.get("lump_description", "").strip() or "ยอดค้างชำระยกมา",
                    "detail": "ประนอมหนี้", "quantity": 1, "unit_label": "รายการ", "unit_price": lump,
                    "amount": lump, "vat_amount": 0.0, "sort_order": 900, "kind": "manual"}
            invoice_id, _ = billing.create_invoice(db, unit, billing.current_period(), [item], settings, today,
                                                   due_date, number_by=today)
            chosen.append(db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone())
        if discount:
            # ส่วนลดหนี้ที่ตกลงกัน: ใส่เป็นรายการติดลบในบิลล่าสุดของแผน
            last = max(chosen, key=lambda i: (i["period"], i["id"]))
            billing.add_manual_item(db, last["id"], "ส่วนลดตามข้อตกลงประนอมหนี้", -discount)
            total = billing.money(total - discount)
        monthly = billing.money(math.ceil(total / installments))
        cur = db.execute(
            "INSERT INTO debt_plans (unit_id, agreement_date, total, installments, monthly, start_period, paid_base,"
            " debtor_name, debtor_phone, note, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (unit["id"], agreement_date, total, installments, monthly, start,
             billing.money(sum(i["paid_amount"] for i in chosen)),
             f.get("debtor_name", "").strip() or unit["owner_name"] or unit["tenant_name"] or "",
             f.get("debtor_phone", "").strip() or unit["phone"] or "", f.get("note", "").strip(), g.user["id"]))
        plan_id = cur.lastrowid
        db.executemany("INSERT INTO debt_plan_invoices (plan_id, invoice_id) VALUES (?,?)",
                       [(plan_id, i["id"]) for i in chosen])
        log_activity(g.user, f"ทำสัญญาประนอมหนี้ห้อง {unit['unit_no']} ยอด {total:,.2f} บาท ผ่อน {installments} งวด")
        db.commit()
        flash(f"บันทึกแผนประนอมหนี้ห้อง {unit['unit_no']} แล้ว ผ่อน {installments} งวด งวดละ {monthly:,.2f} บาท",
              "success")
        return redirect(url_for("admin.debt_detail", plan_id=plan_id))
    return render_template("admin/debt_form.html", unit=unit, invs=invs, form={}, today=date.today().isoformat(),
                           current_period=billing.current_period())


def load_plan(plan_id):
    return get_or_404("SELECT d.*, u.unit_no, u.owner_name, u.tenant_name, u.unit_type FROM debt_plans d"
                      " JOIN units u ON u.id=d.unit_id WHERE d.id=?", (plan_id,))


@bp.route("/debts/<int:plan_id>")
@admin_required
def debt_detail(plan_id):
    db = get_db()
    plan = load_plan(plan_id)
    state = plan_state(db, plan)
    payments = db.execute(
        "SELECT p.*, i.invoice_no FROM payments p JOIN invoices i ON i.id=p.invoice_id"
        " JOIN debt_plan_invoices d ON d.invoice_id=i.id WHERE d.plan_id=? AND p.created_at>=?"
        " ORDER BY p.paid_at, p.id", (plan_id, plan["created_at"])).fetchall()
    return render_template("admin/debt_detail.html", plan=plan, payments=payments, methods=PAYMENT_METHODS,
                           today=date.today().isoformat(), statuses=STATUS, **state)


@bp.route("/debts/<int:plan_id>/print")
@admin_required
def debt_print(plan_id):
    db = get_db()
    plan = load_plan(plan_id)
    return render_template("admin/debt_print.html", plan=plan, **plan_state(db, plan))


@bp.route("/debts/<int:plan_id>/pay", methods=("POST",))
@admin_required
def debt_pay(plan_id):
    """รับเงินผ่อน: ตัดชำระบิลในแผนที่เก่าที่สุดก่อน ออกใบเสร็จตามบิลที่ถูกตัด"""
    db = get_db()
    plan = load_plan(plan_id)
    state = plan_state(db, plan)
    amount = billing.money(to_float(request.form.get("amount")))
    paid_at = request.form.get("paid_at") or date.today().isoformat()
    method = request.form.get("method") if request.form.get("method") in PAYMENT_METHODS else "เงินสด"
    open_invs = [i for i in state["invoices"] if i["status"] in ("unpaid", "partial")]
    left = billing.money(sum(i["total"] - i["paid_amount"] for i in open_invs))
    if plan["status"] != "active":
        flash("แผนนี้ไม่ได้อยู่ระหว่างผ่อน", "error")
        return redirect(url_for("admin.debt_detail", plan_id=plan_id))
    if amount <= 0 or amount > left + 0.005:
        flash(f"จำนวนเงินต้องมากกว่า 0 และไม่เกินยอดคงเหลือ {left:,.2f} บาท", "error")
        return redirect(url_for("admin.debt_detail", plan_id=plan_id))
    nxt = state["next"]
    note = f"ผ่อนประนอมหนี้ งวดที่ {nxt['no']}/{plan['installments']}" if nxt else "ผ่อนประนอมหนี้"
    note = (note + " " + request.form.get("note", "").strip()).strip()
    payment_ids, rest = [], amount
    for inv in open_invs:
        if rest <= 0:
            break
        part = billing.money(min(rest, inv["total"] - inv["paid_amount"]))
        receipt_no = record_payment(db, inv, part, paid_at, method, request.form.get("reference", "").strip(), note)
        payment_ids.append(db.execute("SELECT id FROM payments WHERE receipt_no=?", (receipt_no,)).fetchone()[0])
        rest = billing.money(rest - part)
    if plan_state(db, plan)["remaining"] <= 0.005:
        db.execute("UPDATE debt_plans SET status='done' WHERE id=?", (plan_id,))
    log_activity(g.user, f"รับเงินผ่อนประนอมหนี้ห้อง {plan['unit_no']} {amount:,.2f} บาท")
    db.commit()
    flash(f"รับเงินผ่อน {amount:,.2f} บาท แล้ว ออกใบเสร็จ {len(payment_ids)} ใบ", "success")
    if request.form.get("print_receipt"):
        return redirect(url_for("admin.receipts_print", ids=",".join(map(str, payment_ids))))
    return redirect(url_for("admin.debt_detail", plan_id=plan_id))


@bp.route("/debts/<int:plan_id>/status", methods=("POST",))
@admin_required
def debt_status(plan_id):
    db = get_db()
    plan = load_plan(plan_id)
    status = request.values.get("status")
    if status in STATUS:
        db.execute("UPDATE debt_plans SET status=? WHERE id=?", (status, plan_id))
        log_activity(g.user, f"เปลี่ยนสถานะประนอมหนี้ห้อง {plan['unit_no']} เป็น {STATUS[status]}")
        db.commit()
        flash(f"เปลี่ยนสถานะเป็น \"{STATUS[status]}\" แล้ว", "success")
    return redirect(url_for("admin.debt_detail", plan_id=plan_id))
