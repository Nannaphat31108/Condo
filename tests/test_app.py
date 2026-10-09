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


def test_water_flat_rate_then_per_unit():
    water = ct(method="meter_rate", rate=16, min_charge=65)
    amounts = {u: billing.compute_item(water, {}, reading(100, 100 + u))["amount"] for u in (0, 1, 4, 5, 10)}
    assert amounts == {0: 65, 1: 65, 4: 65, 5: 80, 10: 160}
    assert "เหมาจ่าย" in billing.compute_item(water, {}, reading(0, 3))["detail"]


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


def test_bahttext_and_numbering_helpers():
    assert billing.bahttext(1510) == "หนึ่งพันห้าร้อยสิบบาทถ้วน"
    assert billing.bahttext(21.25) == "ยี่สิบเอ็ดบาทยี่สิบห้าสตางค์"
    assert billing.bahttext(101) == "หนึ่งร้อยเอ็ดบาทถ้วน"
    assert billing.bahttext(1) == "หนึ่งบาทถ้วน"
    assert billing.bahttext(11_000_000) == "สิบเอ็ดล้านบาทถ้วน"
    assert billing.bahttext(0.5) == "ห้าสิบสตางค์"
    assert billing.thai_date("2026-10-09") == "9 ตุลาคม 2569"


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
        # น้ำ 10×16=160, ไฟ 100×8=800, ส่วนกลาง 250, ขยะ 20, ประกัน 10, ค่าซ่อม 250
        assert invs[0]["total"] == 160 + 800 + 250 + 20 + 10 + 250
        assert invs[0]["invoice_no"] == "0001/09/2026"
        water = db.execute("SELECT * FROM invoice_items WHERE invoice_id=? AND description='ค่าน้ำประปา'",
                           (invs[0]["id"],)).fetchone()
        assert (water["meter_prev"], water["meter_curr"]) == (0, 10)

    # จดไฟห้อง 102 แล้วออกบิลซ้ำ -> ได้บิลห้อง 102 เพิ่ม ห้อง 101 ไม่ซ้ำ
    s.post("/admin/meters", {"period": "2026-09", "charge_type_id": cts["ค่าไฟฟ้า"],
                             f"prev_{units['102']}": "0", f"curr_{units['102']}": "50"})
    s.post("/admin/billing", {"period": "2026-09"})
    with app.app_context():
        db = get_db()
        inv102 = db.execute("SELECT * FROM invoices WHERE unit_no='102'").fetchone()
        # น้ำ 2 หน่วย เหมาจ่าย 65, ไฟ 400, ส่วนกลาง 250, ขยะ 20, ประกัน 10, ที่จอดรถ 500
        assert inv102["total"] == 65 + 400 + 250 + 20 + 10 + 500
        assert inv102["invoice_no"] == "0002/09/2026"
        assert db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 2
        inv101 = db.execute("SELECT * FROM invoices WHERE unit_no='101'").fetchone()

    # รับชำระบางส่วน แล้วครบ
    s.post(f"/admin/invoices/{inv101['id']}/pay", {"amount": "300", "paid_at": "2026-09-05"})
    s.post(f"/admin/invoices/{inv101['id']}/pay", {"amount": "1190", "paid_at": "2026-10-06"})
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT status FROM invoices WHERE id=?", (inv101["id"],)).fetchone()[0] == "paid"
        receipts = [r[0] for r in db.execute("SELECT receipt_no FROM payments ORDER BY id")]
        assert receipts == ["0001/09/2026", "0001/10/2026"]

    # งวดถัดไป: เลขครั้งก่อนต้องดึงจากงวดก่อน, เลขบิลรันใหม่ตามเดือน, ไม่มีค่าปรับอัตโนมัติ
    page = client.get(f"/admin/meters?period=2026-10&charge_type_id={cts['ค่าน้ำประปา']}").get_data(as_text=True)
    assert f'name="prev_{units["101"]}" value="10"' in page
    for name in ("ค่าน้ำประปา", "ค่าไฟฟ้า"):
        s.post("/admin/meters", {"period": "2026-10", "charge_type_id": cts[name],
                                 f"prev_{units['101']}": "10", f"curr_{units['101']}": "20",
                                 f"prev_{units['102']}": "2", f"curr_{units['102']}": "60"})
    s.post("/admin/billing", {"period": "2026-10"})
    with app.app_context():
        db = get_db()
        oct_ = {r["unit_no"]: r for r in db.execute("SELECT * FROM invoices WHERE period='2026-10'")}
        assert oct_["101"]["invoice_no"] == "0001/10/2026"
        assert db.execute("SELECT COUNT(*) FROM invoice_items WHERE kind='penalty'").fetchone()[0] == 0
    inv_oct_102 = oct_["102"]

    # แอดมินกรอกเบี้ยปรับเอง (เบี้ยปรับ / เบี้ยปรับค่าน้ำ)
    s.post("/admin/penalties", {"period": "2026-10", f"p_{inv_oct_102['id']}_0": "100",
                                f"p_{inv_oct_102['id']}_1": "50"})
    with app.app_context():
        db = get_db()
        row = db.execute("SELECT * FROM invoices WHERE id=?", (inv_oct_102["id"],)).fetchone()
        assert row["total"] == inv_oct_102["total"] + 150
    # แก้เบี้ยปรับค่าน้ำเป็น 0 = ลบออก, เพิ่มรายการเองจากหน้าบิล
    s.post("/admin/penalties", {"period": "2026-10", f"p_{inv_oct_102['id']}_0": "100",
                                f"p_{inv_oct_102['id']}_1": ""})
    s.post(f"/admin/invoices/{inv_oct_102['id']}/items", {"description": "เบี้ยปรับค่าน้ำ", "amount": "30"})
    with app.app_context():
        db = get_db()
        row = db.execute("SELECT * FROM invoices WHERE id=?", (inv_oct_102["id"],)).fetchone()
        assert row["total"] == inv_oct_102["total"] + 130
        pens = dict(db.execute("SELECT description, amount FROM invoice_items WHERE invoice_id=? AND kind='penalty'",
                               (inv_oct_102["id"],)).fetchall())
        assert pens == {"เบี้ยปรับ": 100, "เบี้ยปรับค่าน้ำ": 30}
    page = client.get(f"/admin/invoices/{inv_oct_102['id']}").get_data(as_text=True)
    assert billing.bahttext(inv_oct_102["total"] + 130) in page

    # ยกเลิกบิล -> ออกบิลใหม่ได้ ด้วยเลขที่ถัดไป
    s.post(f"/admin/invoices/{inv_oct_102['id']}/void")
    s.post("/admin/billing", {"period": "2026-10"})
    with app.app_context():
        db = get_db()
        new = db.execute("SELECT * FROM invoices WHERE period='2026-10' AND unit_no='102' AND status!='void'").fetchall()
        assert len(new) == 1 and new[0]["invoice_no"] == "0003/10/2026"

    # ทุกหน้าของแอดมินเปิดได้
    for url in ["/admin/", "/admin/units", f"/admin/units/{units['101']}", "/admin/users", "/admin/charges",
                "/admin/charges/new", f"/admin/charges/{cts['ค่าไฟฟ้า']}/edit", "/admin/meters", "/admin/adhoc",
                "/admin/billing?period=2026-10", "/admin/invoices", "/admin/invoices?status=outstanding",
                f"/admin/invoices/{inv101['id']}", "/admin/invoices/print?period=2026-10", "/admin/reports?year=2026",
                "/admin/settings", "/admin/activity", "/admin/payments/1/receipt", "/admin/export/invoices.csv",
                "/admin/export/items.csv", "/admin/export/payments.csv", "/admin/backup", "/admin/units/import",
                "/admin/users/new", "/admin/penalties?period=2026-10", "/admin/payments/2/receipt",
                "/admin/charges/preview?method=meter_tiered&tiers=[{\"upto\":10,\"rate\":5}]&usage=12"]:
        assert client.get(url).status_code == 200, url

    # ลูกบ้าน: เห็นเฉพาะบิลห้องตัวเอง
    s.post("/admin/users/new", {"username": "room101", "password": "secret1", "role": "resident",
                                "unit_id": units["101"], "active": "1"})
    s.post("/logout")
    s.login("room101", "secret1")
    assert "0001/09/2026" in client.get("/my/").get_data(as_text=True)
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


def test_generate_rooms_bulk_payment_and_receipt_print(app, client):
    s = Session(client)
    s.login("admin", "admin123")
    # 198 ห้อง: ชั้น 1-9 ชั้นละ 22 ห้อง
    s.post("/admin/units/import", {"mode": "generate", "floor_from": "1", "floor_to": "9", "per_floor": "22",
                                   "digits": "2", "building": "A"})
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 198
        assert {"101", "122", "922"} <= {r[0] for r in db.execute("SELECT unit_no FROM units")}
        db.execute("UPDATE charge_types SET active=0 WHERE method LIKE 'meter%'")
        db.commit()
    s.post("/admin/billing", {"period": "2026-10"})
    with app.app_context():
        db = get_db()
        invs = db.execute("SELECT * FROM invoices ORDER BY unit_no").fetchall()
        assert len(invs) == 198 and invs[0]["total"] == 280
        assert invs[-1]["invoice_no"] == "0198/10/2026"

    form = {"period": "2026-10", "paid_at": "2026-10-05", "method": "เงินสด"}
    for inv in invs[:3]:
        form[f"pay_{inv['id']}"] = "1"
        form[f"amount_{inv['id']}"] = "280"
    form[f"pay_{invs[3]['id']}"] = "1"
    form[f"amount_{invs[3]['id']}"] = "100"   # ชำระบางส่วน
    form[f"amount_{invs[4]['id']}"] = "280"   # ไม่ได้ติ๊ก -> ไม่บันทึก
    s.post("/admin/payments/bulk", form)
    with app.app_context():
        db = get_db()
        rc = [r[0] for r in db.execute("SELECT receipt_no FROM payments ORDER BY id")]
        assert rc == ["0001/10/2026", "0002/10/2026", "0003/10/2026", "0004/10/2026"]
        statuses = [db.execute("SELECT status FROM invoices WHERE id=?", (i["id"],)).fetchone()[0] for i in invs[:5]]
        assert statuses == ["paid", "paid", "paid", "partial", "unpaid"]

    for url in ["/admin/payments/bulk?period=2026-10", "/admin/receipts/print?period=2026-10",
                "/admin/receipts/print?period=2026-10&by=paid&layout=half", "/admin/units/import"]:
        r = client.get(url)
        assert r.status_code == 200, url
    page = client.get("/admin/receipts/print?period=2026-10&layout=half").get_data(as_text=True)
    assert page.count('class="half-sheet"') == 4 and "0004/10/2026" in page


def test_example_settings_migrate_and_meter_dates(app, client):
    with app.app_context():
        db = get_db()
        db.execute("UPDATE settings SET value='นิติบุคคลอาคารชุด ตัวอย่างคอนโด' WHERE key='condo_name'")
        db.execute("UPDATE settings SET value='' WHERE key='phone'")
        db.commit()
        from condo.db import init_db
        init_db(db)
        settings = dict(db.execute("SELECT key, value FROM settings").fetchall())
        assert settings["condo_name"] == "นิติบุคคลอาคารชุดเคหะชุมชนคลองจั่น 26"
        assert settings["phone"] == "02-3755395"
    s = Session(client)
    s.login("admin", "admin123")
    s.post("/admin/units/new", {"unit_no": "26/94", "active": "1"})
    with app.app_context():
        db = get_db()
        unit_id = db.execute("SELECT id FROM units").fetchone()[0]
        water = db.execute("SELECT id FROM charge_types WHERE name='ค่าน้ำประปา'").fetchone()[0]
        db.execute("UPDATE charge_types SET active=0 WHERE method LIKE 'meter%' AND id!=?", (water,))
        db.commit()
    for period, date_, prev, curr in (("2026-09", "2026-09-08", 0, 10), ("2026-10", "2026-10-08", 10, 25)):
        s.post("/admin/meters", {"period": period, "charge_type_id": water, "read_date": date_,
                                 f"prev_{unit_id}": prev, f"curr_{unit_id}": curr})
    s.post("/admin/billing", {"period": "2026-10"})
    with app.app_context():
        it = get_db().execute("SELECT * FROM invoice_items WHERE description='ค่าน้ำประปา'").fetchone()
        assert (it["meter_prev_date"], it["meter_curr_date"]) == ("2026-09-08", "2026-10-08")
        inv_id = it["invoice_id"]
    page = client.get(f"/admin/invoices/{inv_id}").get_data(as_text=True)
    assert "จดครั้งก่อน 8 ก.ย. 69" in page and "จดครั้งหลัง 8 ต.ค. 69" in page and "logo.png" in page


def test_owner_and_tenant_on_bill(app, client):
    s = Session(client)
    s.login("admin", "admin123")
    s.post("/admin/units/new", {"unit_no": "26/94", "owner_name": "สมชาย เจ้าของ", "phone": "0811111111",
                                "tenant_name": "สมศรี ผู้เช่า", "tenant_phone": "0822222222", "active": "1"})
    s.post("/admin/units/import", {"csv_text": "26/95,26,3,30,มานะ,081,ปิติ,082\n26/96,26,3,30,ชูใจ,083\n"})
    with app.app_context():
        db = get_db()
        rows = {r["unit_no"]: r for r in db.execute("SELECT * FROM units")}
        assert (rows["26/94"]["tenant_name"], rows["26/94"]["tenant_phone"]) == ("สมศรี ผู้เช่า", "0822222222")
        assert rows["26/95"]["tenant_name"] == "ปิติ" and rows["26/96"]["tenant_name"] == ""
        db.execute("UPDATE charge_types SET active=0 WHERE method LIKE 'meter%'")
        db.commit()
    assert "สมศรี ผู้เช่า" in client.get("/admin/units?q=สมศรี").get_data(as_text=True)
    s.post("/admin/billing", {"period": "2026-10"})
    with app.app_context():
        inv = get_db().execute("SELECT * FROM invoices WHERE unit_no='26/94'").fetchone()
        assert (inv["owner_name"], inv["tenant_name"]) == ("สมชาย เจ้าของ", "สมศรี ผู้เช่า")
    page = client.get(f"/admin/invoices/{inv['id']}").get_data(as_text=True)
    assert "เจ้าของห้องชุด" in page and "ผู้เช่า" in page and "สมศรี ผู้เช่า" in page
    assert 'name="email"' not in client.get("/admin/units/new").get_data(as_text=True)


def test_sequential_rooms_khlong_chan_26(app, client):
    s = Session(client)
    s.login("admin", "admin123")
    s.post("/admin/units/import", {"mode": "sequence", "prefix": "26/", "start_no": "1", "first_floor": "1",
                                   "building": "26", "floor_counts": "22,44,44,44,44"})
    with app.app_context():
        rows = get_db().execute("SELECT unit_no, floor FROM units ORDER BY length(unit_no), unit_no").fetchall()
    units = {r["unit_no"]: r["floor"] for r in rows}
    assert len(units) == 198
    assert (units["26/1"], units["26/22"], units["26/23"], units["26/66"]) == ("1", "1", "2", "2")
    assert (units["26/67"], units["26/110"], units["26/111"], units["26/155"], units["26/198"]) == ("3", "3", "4", "5", "5")
    assert [r["unit_no"] for r in rows][:3] == ["26/1", "26/2", "26/3"]
    page = client.get("/admin/units").get_data(as_text=True)
    assert page.index(">26/2<") < page.index(">26/10<") < page.index(">26/100<")
