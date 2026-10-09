"""โปรแกรมบริหารนิติบุคคลอาคารชุด / หมู่บ้าน"""
import os
import secrets

from flask import Flask, abort, request, session

from . import billing, db


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("CONDO_SECRET_KEY"),
        DATABASE=os.environ.get("CONDO_DATABASE", os.path.join(app.instance_path, "condo.sqlite3")),
        # บนเซิร์ฟเวอร์ที่ใช้ HTTPS (เช่น Render) ให้ส่ง cookie ผ่าน HTTPS เท่านั้น
        SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER") or os.environ.get("CONDO_HTTPS")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(app.config["DATABASE"])), exist_ok=True)

    if not app.config["SECRET_KEY"]:
        # เก็บ secret key ไว้ในโฟลเดอร์ instance เพื่อให้ session ไม่หลุดเมื่อรีสตาร์ท
        key_file = os.path.join(app.instance_path, "secret_key")
        if not os.path.exists(key_file):
            with open(key_file, "w") as f:
                f.write(secrets.token_hex(32))
        with open(key_file) as f:
            app.config["SECRET_KEY"] = f.read().strip()

    app.teardown_appcontext(db.close_db)
    with app.app_context():
        db.init_db(db.get_db())

    @app.before_request
    def csrf_protect():
        if request.method == "POST":
            token = session.get("_csrf")
            if not token or request.form.get("_csrf") != token:
                abort(400, "CSRF token ไม่ถูกต้อง กรุณาโหลดหน้าใหม่แล้วลองอีกครั้ง")

    def csrf_token():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_hex(16)
        return session["_csrf"]

    # globals ใช้ได้ทั้งในเทมเพลตและ macro ที่ import เข้ามา
    app.jinja_env.globals.update(
        csrf_token=csrf_token,
        METHOD_LABELS=billing.METHOD_LABELS,
        FREQUENCY_LABELS=billing.FREQUENCY_LABELS,
        STATUS_LABELS=billing.STATUS_LABELS,
        THAI_MONTHS=billing.THAI_MONTHS,
    )

    @app.context_processor
    def inject_settings():
        return {"settings": db.get_settings()}

    @app.template_filter("baht")
    def baht(value):
        return f"{float(value or 0):,.2f}"

    @app.template_filter("num")
    def num(value):
        return billing.fmt_num(value or 0)

    app.add_template_filter(billing.period_label, "period")
    app.add_template_filter(billing.bahttext, "bahttext")
    app.add_template_filter(billing.thai_date, "thaidate")
    app.add_template_filter(billing.thai_date_short, "thaidate_short")

    from . import auth, views_admin, views_resident
    app.register_blueprint(auth.bp)
    app.register_blueprint(views_admin.bp)
    app.register_blueprint(views_resident.bp)
    app.add_url_rule("/", endpoint="index", view_func=auth.index)
    return app
