"""รายจ่ายของนิติบุคคล (รายจ่ายส่วนกลาง / รายจ่ายพิเศษ) และสรุปรายรับ-รายจ่าย รายวัน รายเดือน รายปี"""
import calendar
from datetime import date

from flask import flash, g, redirect, render_template, request, url_for

from . import billing
from .auth import admin_required
from .db import get_db, get_settings, log_activity
from .views_admin import PAYMENT_METHODS, bp, csv_response, get_or_404, to_float, valid_period

CATEGORIES = {"common": "รายจ่ายส่วนกลาง", "special": "รายจ่ายพิเศษ"}


def expense_items():
    s = get_settings()
    return {k: [x.strip() for x in (s.get(f"expense_items_{k}") or "").splitlines() if x.strip()] for k in CATEGORIES}


def expense_form_data(f):
    data = {
        "expense_date": f.get("expense_date") or date.today().isoformat(),
        "category": f.get("category") if f.get("category") in CATEGORIES else "common",
        "item": f.get("item", "").strip(),
        "description": f.get("description", "").strip(),
        "amount": billing.money(to_float(f.get("amount"))),
        "payee": f.get("payee", "").strip(),
        "method": f.get("method", "").strip(),
        "reference": f.get("reference", "").strip(),
    }
    try:
        date.fromisoformat(data["expense_date"])
    except ValueError:
        return data, "วันที่ไม่ถูกต้อง"
    if not data["item"]:
        return data, "กรุณากรอกรายการ"
    if data["amount"] <= 0:
        return data, "จำนวนเงินต้องมากกว่า 0"
    return data, None


@bp.route("/expenses", methods=("GET", "POST"))
@admin_required
def expenses():
    db = get_db()
    month = request.values.get("month") if valid_period(request.values.get("month")) else billing.current_period()
    category = request.values.get("category") if request.values.get("category") in CATEGORIES else ""
    if request.method == "POST":
        data, error = expense_form_data(request.form)
        if error:
            flash(error, "error")
        else:
            db.execute("INSERT INTO expenses (expense_date, category, item, description, amount, payee, method, reference,"
                       " created_by) VALUES (?,?,?,?,?,?,?,?,?)", (*data.values(), g.user["id"]))
            log_activity(g.user, f"บันทึก{CATEGORIES[data['category']]} {data['item']} {data['amount']:,.2f} บาท")
            db.commit()
            flash(f"บันทึก{CATEGORIES[data['category']]} \"{data['item']}\" {data['amount']:,.2f} บาท แล้ว", "success")
        return redirect(url_for("admin.expenses", month=data["expense_date"][:7], category=category or None))
    sql, params = "SELECT * FROM expenses WHERE substr(expense_date,1,7)=?", [month]
    if category:
        sql += " AND category=?"
        params.append(category)
    rows = db.execute(sql + " ORDER BY expense_date DESC, id DESC", params).fetchall()
    totals = {k: billing.money(sum(r["amount"] for r in rows if r["category"] == k)) for k in CATEGORIES}
    return render_template("admin/expenses.html", rows=rows, month=month, category=category, totals=totals,
                           categories=CATEGORIES, items=expense_items(), methods=PAYMENT_METHODS,
                           today=date.today().isoformat())


@bp.route("/expenses/<int:expense_id>/edit", methods=("GET", "POST"))
@admin_required
def expense_edit(expense_id):
    db = get_db()
    row = get_or_404("SELECT * FROM expenses WHERE id=?", (expense_id,))
    if request.method == "POST":
        data, error = expense_form_data(request.form)
        if error:
            flash(error, "error")
            row = {**dict(row), **data}
        else:
            db.execute("UPDATE expenses SET expense_date=?, category=?, item=?, description=?, amount=?, payee=?,"
                       " method=?, reference=? WHERE id=?", (*data.values(), expense_id))
            log_activity(g.user, f"แก้ไขรายจ่าย {data['item']} {data['amount']:,.2f} บาท")
            db.commit()
            flash("แก้ไขรายจ่ายแล้ว", "success")
            return redirect(url_for("admin.expenses", month=data["expense_date"][:7]))
    return render_template("admin/expense_form.html", row=row, categories=CATEGORIES, items=expense_items(),
                           methods=PAYMENT_METHODS)


@bp.route("/expenses/<int:expense_id>/delete", methods=("POST",))
@admin_required
def expense_delete(expense_id):
    db = get_db()
    row = get_or_404("SELECT * FROM expenses WHERE id=?", (expense_id,))
    db.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
    log_activity(g.user, f"ลบรายจ่าย {row['item']} {row['amount']:,.2f} บาท ({row['expense_date']})")
    db.commit()
    flash("ลบรายจ่ายแล้ว", "success")
    return redirect(url_for("admin.expenses", month=row["expense_date"][:7]))


def summarize(db, key_len, start, end):
    """รวมรายรับ (ใบเสร็จ) และรายจ่าย ตามวัน/เดือน/ปี (key_len 10 / 7 / 4 ตัวอักษรของวันที่)"""
    rows = {}

    def slot(key):
        return rows.setdefault(key, {"income": 0.0, "common": 0.0, "special": 0.0})

    for r in db.execute(f"SELECT substr(paid_at,1,{key_len}) AS k, SUM(amount) FROM payments"
                        " WHERE paid_at BETWEEN ? AND ? GROUP BY k", (start, end)):
        slot(r[0])["income"] = billing.money(r[1])
    for r in db.execute(f"SELECT substr(expense_date,1,{key_len}) AS k, category, SUM(amount) FROM expenses"
                        " WHERE expense_date BETWEEN ? AND ? GROUP BY k, category", (start, end)):
        if r[1] in CATEGORIES:
            slot(r[0])[r[1]] = billing.money(r[2])
    out = []
    for k in sorted(rows):
        v = rows[k]
        expense = billing.money(v["common"] + v["special"])
        out.append({"key": k, **v, "expense": expense, "net": billing.money(v["income"] - expense)})
    return out


@bp.route("/expenses/summary")
@admin_required
def expense_summary():
    db = get_db()
    view = request.args.get("view") if request.args.get("view") in ("day", "month", "year") else "month"
    today = date.today()
    month = request.args.get("month") if valid_period(request.args.get("month")) else today.strftime("%Y-%m")
    year = request.args.get("year", type=int) or today.year
    if view == "day":
        y, m = (int(x) for x in month.split("-"))
        start, end, key_len = f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}", 10
    elif view == "month":
        start, end, key_len = f"{year}-01-01", f"{year}-12-31", 7
    else:
        start, end, key_len = "1900-01-01", "9999-12-31", 4
    rows = summarize(db, key_len, start, end)
    totals = {k: billing.money(sum(r[k] for r in rows)) for k in ("income", "common", "special", "expense", "net")}
    by_item = db.execute(
        "SELECT category, item, SUM(amount) AS total, COUNT(*) AS cnt FROM expenses WHERE expense_date BETWEEN ? AND ?"
        " GROUP BY category, item ORDER BY category, total DESC", (start, end)).fetchall()
    if request.args.get("export") == "csv":
        return csv_response(
            f"income-expense-{view}-{month if view == 'day' else year if view == 'month' else 'all'}.csv",
            ["วัน/เดือน/ปี", "รายรับ", "รายจ่ายส่วนกลาง", "รายจ่ายพิเศษ", "รวมรายจ่าย", "คงเหลือสุทธิ"],
            [[r["key"], r["income"], r["common"], r["special"], r["expense"], r["net"]] for r in rows]
            + [["รวม", totals["income"], totals["common"], totals["special"], totals["expense"], totals["net"]]],
        )
    years = [r[0] for r in db.execute(
        "SELECT DISTINCT y FROM (SELECT substr(paid_at,1,4) AS y FROM payments UNION"
        " SELECT substr(expense_date,1,4) FROM expenses) ORDER BY y DESC") if r[0]]
    if str(year) not in years:
        years.insert(0, str(year))
    return render_template("admin/expense_summary.html", view=view, month=month, year=year, years=years, rows=rows,
                           totals=totals, by_item=by_item, categories=CATEGORIES)


def label_for(key):
    """'2026-10-05' -> '5 ตุลาคม 2569', '2026-10' -> 'ตุลาคม 2569', '2026' -> 'พ.ศ. 2569'"""
    if len(key) == 10:
        return billing.thai_date(key)
    if len(key) == 7:
        return billing.period_label(key)
    return f"พ.ศ. {int(key) + 543}"


bp.add_app_template_filter(label_for, "sumlabel")
