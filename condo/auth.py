"""ระบบ login / logout / เปลี่ยนรหัสผ่าน และตัวตรวจสิทธิ์"""
import functools

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db, log_activity

bp = Blueprint("auth", __name__)

MIN_PASSWORD_LENGTH = 6


@bp.before_app_request
def load_user():
    user_id = session.get("user_id")
    g.user = None
    if user_id is not None:
        g.user = get_db().execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
        if g.user is None:
            session.clear()


def login_required(view):
    @functools.wraps(view)
    def wrapped(**kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.path))
        return view(**kwargs)
    return wrapped


def admin_required(view):
    @functools.wraps(view)
    def wrapped(**kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.path))
        if g.user["role"] != "admin":
            flash("หน้านี้สำหรับผู้ดูแลระบบเท่านั้น", "error")
            return redirect(url_for("resident.home"))
        return view(**kwargs)
    return wrapped


def index():
    if g.user is None:
        return redirect(url_for("auth.login"))
    if g.user["role"] == "admin":
        return redirect(url_for("admin.dashboard"))
    return redirect(url_for("resident.home"))


@bp.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user is None:  # มือถือมักพิมพ์ตัวแรกเป็นตัวใหญ่ให้เอง
            user = db.execute("SELECT * FROM users WHERE lower(username)=lower(?)", (username,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", "error")
        elif not user["active"]:
            flash("บัญชีนี้ถูกปิดการใช้งาน กรุณาติดต่อนิติบุคคล", "error")
        else:
            csrf = session.get("_csrf")
            session.clear()
            session["user_id"] = user["id"]
            if csrf:
                session["_csrf"] = csrf
            db.execute("UPDATE users SET last_login=datetime('now','localtime') WHERE id=?", (user["id"],))
            log_activity(user, "เข้าสู่ระบบ")
            db.commit()
            next_url = request.args.get("next", "")
            if next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect(url_for("index"))
    return render_template("login.html")


@bp.route("/logout", methods=("POST",))
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/account/password", methods=("GET", "POST"))
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not check_password_hash(g.user["password_hash"], current):
            flash("รหัสผ่านปัจจุบันไม่ถูกต้อง", "error")
        elif len(new) < MIN_PASSWORD_LENGTH:
            flash(f"รหัสผ่านใหม่ต้องมีอย่างน้อย {MIN_PASSWORD_LENGTH} ตัวอักษร", "error")
        elif new != confirm:
            flash("ยืนยันรหัสผ่านใหม่ไม่ตรงกัน", "error")
        else:
            db = get_db()
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new), g.user["id"]))
            log_activity(g.user, "เปลี่ยนรหัสผ่าน")
            db.commit()
            flash("เปลี่ยนรหัสผ่านเรียบร้อยแล้ว", "success")
            return redirect(url_for("index"))
    return render_template("change_password.html")
