import re

import pytest

from condo import billing, create_app
from condo.db import get_db


# ---------------------------------------------------------------- unit tests: calculation
def ct(**kw):
    base = dict(id=1, name="x", method="fixed", rate=0, tiers="[]", fixed_fee=0, min_charge=0,
                unit_label="หน่วย", vat_percent=0, sort_order=0)
    base.update(kw)
    return base


def reading(prev, curr):
    return {"prev_reading": prev, "curr_reading": curr}


def test_meter_rate_with_min_charge():
    water = ct(method="meter_rate", rate=18, min_charge=100)
    assert billing.compute_item(water, {}, reading(100, 110))["amount"] == 180
    assert billing.compute_item(water, {}, reading(100, 102))["amount"] == 100
    assert billing.compute_item(water, {}, None) is None


def test_meter_tiered_progressive():
    tiers = [{"upto": 10, "rate": 10}, {"upto": 30, "rate": 15}, {"upto": None, "rate": 20}]
    elec = ct(method="meter_tiered", tiers=tiers, fixed_fee=20)
    assert billing.compute_item(elec, {}, reading(0, 35))["amount"] == 10 * 10 + 20 * 15 + 5 * 20 + 20
    assert billing.compute_item(elec, {}, reading(0, 5))["amount"] == 50 + 20
    # ขั้นสุดท้ายมีเพดาน: หน่วยที่เกินคิดด้วยอัตราขั้นสุดท้าย
    capped = ct(method="meter_tiered", tiers=[{"upto": 10, "rate": 1}, {"upto": 20, "rate": 2}])
    assert billing.compute_item(capped, {}, reading(0, 25))["amount"] == 10 + 15 * 2


def test_fixed_per_area_vat_and_override():
    assert billing.compute_item(ct(rate=30), {})["amount"] == 30
    area = billing.compute_item(ct(method="per_area", rate=35, vat_percent=7), {"area_sqm": 30})
    assert area["amount"] == 1050 and area["vat_amount"] == 73.5
    assert billing.compute_item(ct(rate=30), {}, override=99)["amount"] == 99


def test_charge_applies_frequency_and_window():
    row = dict(active=1, start_period=None, end_period=None, frequency="monthly", bill_month=None, apply_to="all")
    assert billing.charge_applies(row, "2026-05", 1, {})
    assert not billing.charge_applies({**row, "frequency": "yearly", "bill_month": 1}, "2026-05", 1, {})
    assert billing.charge_applies({**row, "frequency": "yearly", "bill_month": 5}, "2026-05", 1, {})
    assert billing.charge_applies({**row, "frequency": "once", "start_period": "2026-05"}, "2026-05", 1, {})
    assert not billing.charge_applies({**row, "frequency": "once", "start_period": "2026-05"}, "2026-06", 1, {})
    assert not billing.charge_applies({**row, "end_period": "2026-04"}, "2026-05", 1, {})
    assert not billing.charge_applies({**row, "apply_to": "selected"}, "2026-05", 1, {2: None})
    assert billing.charge_applies({**row, "apply_to": "selected"}, "2026-05", 2, {2: None})


def test_period_helpers():
    assert billing.period_label("2026-10") == "ตุลาคม 2569"
    assert billing.prev_period("2026-01") == "2025-12"


# ---------------------------------------------------------------- integration tests
@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.sqlite3"), "SECRET_KEY": "test"})


@pytest.fixture
def client(app):
    return app.test_client()


class Session:
    def __init__(self, client):
        self.c = client

    def token(self):
        html = self.c.get("/login").get_data(as_text=True)
        found = re.search(r'name="_csrf" value="([^"]+)"', html)
        if found:
            return found.group(1)
        with self.c.session_transaction() as s:
            return s["_csrf"]

    def post(self, url, data=None, **kw):
        data = dict(data or {})
        data["_csrf"] = self.token()
        return self.c.post(url, data=data, **kw)

    def login(self, username, password):
        return self.post("/login", {"username": username, "password": password})


def test_login_required_and_csrf(client):
    assert client.get("/admin/").status_code == 302
    assert client.post("/login", data={"username": "admin", "password": "admin123"}).status_code == 400
    s = Session(client)
    assert s.login("admin", "wrong").status_code == 200
    assert s.login("admin", "admin123").headers["Location"] == "/"
    assert client.get("/admin/").status_code == 200


def test_full_billing_flow(app, client):
    s = Session(client)
    s.login("admin", "admin123")
    s.post("/admin/units/import", {"csv_text": "101,A,1,30,สมชาย,081\n102,A,1,40,สมหญิง,082\n"})
    with app.app_context():
        db = get_db()
        units = {r["unit_no"]: r["id"] for r in db.execute("SELECT * FROM units")}
        cts = {r["name"]: r["id"] for r in db.execute("SELECT * FROM charge_types")}
    assert set(units) == {"101", "102"}

    # เพิ่มค่าบริการใหม่เฉพาะห้อง 102: ค่าที่จอดรถ 500
    s.post("/admin/charges/new", {"name": "ค่าที่จอดรถ", "method": "fixed", "rate": "500", "frequency": "monthly",
                                  "apply_to": "selected", f"unit_{units['102']}": "1", "active": "1",
                                  "sort_order": "60", "unit_label": "คัน"})
    # จดมิเตอร์ น้ำ + ไฟ (ห้อง 102 ยังไม่จดไฟ)
    s.post("/admin/meters", {"period": "2026-09", "charge_type_id": cts["ค่าน้ำประปา"],
                             f"prev_{units['101']}": "0", f"curr_{units['101']}": "10",
                             f"prev_{units['102']}": "0", f"curr_{units['102']}": "2"})
    s.post("/admin/meters", {"period": "2026-09", "charge_type_id": cts["ค่าไฟฟ้า"],
                             f"prev_{units['101']}": "0", f"curr_{units['101']}": "100"})
    s.post("/admin/adhoc", {"period": "2026-09", "description": "ค่าซ่อม", "amount": "250", "unit_ids": [units["101"]]})

    r = s.post("/admin/billing", {"period": "2026-09"}, follow_redirects=True)
    assert "ยังไม่จดมิเตอร์" in r.get_data(as_text=True)
    with app.app_context():
        db = get_db()
        invs = db.execute("SELECT * FROM invoices").fetchall()
        assert [i["unit_no"] for i in invs] == ["101"]
        # น้ำ 10×18=180, ไฟ 100×8=800, ขยะ 30, ประกัน 50, ค่าซ่อม 250
        assert invs[0]["total"] == 180 + 800 + 30 + 50 + 250

    # จดไฟห้อง 102 แล้วออกบิลซ้ำ -> ได้บิลห้อง 102 เพิ่ม ห้อง 101 ไม่ซ้ำ
    s.post("/admin/meters", {"period": "2026-09", "charge_type_id": cts["ค่าไฟฟ้า"],
                             f"prev_{units['102']}": "0", f"curr_{units['102']}": "50"})
    s.post("/admin/billing", {"period": "2026-09"})
    with app.app_context():
        db = get_db()
        inv102 = db.execute("SELECT * FROM invoices WHERE unit_no='102'").fetchone()
        # น้ำขั้นต่ำ 100, ไฟ 400, ขยะ 30, ประกัน 50, ที่จอดรถ 500
        assert inv102["total"] == 100 + 400 + 30 + 50 + 500
        assert db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 2
        inv101 = db.execute("SELECT * FROM invoices WHERE unit_no='101'").fetchone()

    # รับชำระบางส่วน แล้วครบ
    s.post(f"/admin/invoices/{inv101['id']}/pay", {"amount": "300", "paid_at": "2026-09-05"})
    s.post(f"/admin/invoices/{inv101['id']}/pay", {"amount": "1010", "paid_at": "2026-09-06"})
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT status FROM invoices WHERE id=?", (inv101["id"],)).fetchone()[0] == "paid"
        assert db.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 2

    # งวดถัดไป: เลขครั้งก่อนต้องดึงจากงวดก่อน และห้อง 102 ค้างเกินกำหนด -> ค่าปรับ 100
    page = client.get(f"/admin/meters?period=2026-10&charge_type_id={cts['ค่าน้ำประปา']}").get_data(as_text=True)
    assert f'name="prev_{units["101"]}" value="10"' in page
    for name in ("ค่าน้ำประปา", "ค่าไฟฟ้า"):
        s.post("/admin/meters", {"period": "2026-10", "charge_type_id": cts[name],
                                 f"prev_{units['101']}": "10", f"curr_{units['101']}": "20",
                                 f"prev_{units['102']}": "2", f"curr_{units['102']}": "60"})
    s.post("/admin/billing", {"period": "2026-10"})
    with app.app_context():
        db = get_db()
        late = db.execute("SELECT ii.* FROM invoice_items ii JOIN invoices i ON i.id=ii.invoice_id"
                          " WHERE i.period='2026-10' AND i.unit_no='102' AND ii.description='ค่าปรับชำระล่าช้า'").fetchall()
        assert len(late) == 1 and late[0]["amount"] == 100
        inv_oct_102 = db.execute("SELECT id FROM invoices WHERE period='2026-10' AND unit_no='102'").fetchone()[0]

    # ยกเลิกบิล -> ค่าปรับคืนสถานะ ออกบิลใหม่ได้
    s.post(f"/admin/invoices/{inv_oct_102}/void")
    s.post("/admin/billing", {"period": "2026-10"})
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT COUNT(*) FROM invoices WHERE period='2026-10' AND unit_no='102'"
                          " AND status!='void'").fetchone()[0] == 1

    # ทุกหน้าของแอดมินเปิดได้
    for url in ["/admin/", "/admin/units", f"/admin/units/{units['101']}", "/admin/users", "/admin/charges",
                "/admin/charges/new", f"/admin/charges/{cts['ค่าไฟฟ้า']}/edit", "/admin/meters", "/admin/adhoc",
                "/admin/billing?period=2026-10", "/admin/invoices", "/admin/invoices?status=outstanding",
                f"/admin/invoices/{inv101['id']}", "/admin/invoices/print?period=2026-10", "/admin/reports?year=2026",
                "/admin/settings", "/admin/activity", "/admin/payments/1/receipt", "/admin/export/invoices.csv",
                "/admin/export/items.csv", "/admin/export/payments.csv", "/admin/backup", "/admin/units/import",
                "/admin/users/new",
                "/admin/charges/preview?method=meter_tiered&tiers=[{\"upto\":10,\"rate\":5}]&usage=12"]:
        assert client.get(url).status_code == 200, url

    # ลูกบ้าน: เห็นเฉพาะบิลห้องตัวเอง
    s.post("/admin/users/new", {"username": "room101", "password": "secret1", "role": "resident",
                                "unit_id": units["101"], "active": "1"})
    s.post("/logout")
    s.login("room101", "secret1")
    assert "INV202609" in client.get("/my/").get_data(as_text=True)
    assert client.get(f"/my/invoices/{inv101['id']}").status_code == 200
    assert client.get(f"/my/invoices/{inv102['id']}").status_code == 404
    assert client.get("/admin/").status_code == 302


def test_cannot_edit_meter_after_invoiced(app, client):
    s = Session(client)
    s.login("admin", "admin123")
    s.post("/admin/units/new", {"unit_no": "201", "area_sqm": "30", "active": "1"})
    with app.app_context():
        db = get_db()
        unit_id = db.execute("SELECT id FROM units").fetchone()[0]
        db.execute("UPDATE charge_types SET active=0 WHERE method LIKE 'meter%' AND name!='ค่าน้ำประปา'")
        db.commit()
        water = db.execute("SELECT id FROM charge_types WHERE name='ค่าน้ำประปา'").fetchone()[0]
    data = {"period": "2026-09", "charge_type_id": water, f"prev_{unit_id}": "0", f"curr_{unit_id}": "10"}
    s.post("/admin/meters", data)
    s.post("/admin/billing", {"period": "2026-09"})
    r = s.post("/admin/meters", {**data, f"curr_{unit_id}": "20"}, follow_redirects=True)
    assert "ต้องยกเลิกบิลก่อน" in r.get_data(as_text=True)
