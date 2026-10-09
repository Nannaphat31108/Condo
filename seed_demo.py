"""สร้างข้อมูลตัวอย่างสำหรับทดลองใช้งาน: python seed_demo.py

เพิ่มห้องตัวอย่าง 12 ห้อง จดมิเตอร์ 3 เดือนย้อนหลัง ออกบิล และบันทึกรับชำระบางส่วน
"""
import random

from werkzeug.security import generate_password_hash

from condo import billing, create_app
from condo.db import get_db, get_settings

NAMES = ["สมชาย ใจดี", "สมหญิง รักสงบ", "วิชัย มั่นคง", "มาลี สวยงาม", "ประเสริฐ ทองคำ", "สุดา แสงจันทร์",
         "อนันต์ ศรีสุข", "กานดา พูนผล", "ธีระ วงศ์ใหญ่", "นภา ฟ้าใส", "ชัยวัฒน์ เจริญ", "พิมพ์ใจ ดวงดี"]


def main():
    app = create_app()
    with app.app_context():
        db = get_db()
        if db.execute("SELECT COUNT(*) FROM units").fetchone()[0]:
            print("มีข้อมูลห้องอยู่แล้ว ข้ามการสร้างข้อมูลตัวอย่าง")
            return
        rnd = random.Random(7)
        for i, name in enumerate(NAMES):
            floor = i // 4 + 1
            db.execute("INSERT INTO units (unit_no, building, floor, area_sqm, owner_name, phone) VALUES (?,?,?,?,?,?)",
                       (f"{floor}0{i % 4 + 1}", "A", str(floor), rnd.choice([28, 32, 35, 45]), name,
                        f"08{rnd.randint(10000000, 99999999)}"))
        first = db.execute("SELECT id FROM units ORDER BY unit_no LIMIT 1").fetchone()[0]
        db.execute("INSERT INTO users (username, password_hash, full_name, role, unit_id) VALUES (?,?,?,?,?)",
                   ("room101", generate_password_hash("room101"), NAMES[0], "resident", first))
        db.commit()

        meters = db.execute("SELECT * FROM charge_types WHERE method LIKE 'meter%'").fetchall()
        units = db.execute("SELECT * FROM units").fetchall()
        current = billing.current_period()
        periods = [current]
        for _ in range(3):
            periods.insert(0, billing.prev_period(periods[0]))
        last = {}
        for period in periods[:-1]:
            for m in meters:
                for u in units:
                    prev = last.get((m["id"], u["id"]), rnd.randint(100, 900))
                    curr = prev + (rnd.randint(3, 15) if "น้ำ" in m["name"] else rnd.randint(80, 300))
                    db.execute("INSERT INTO meter_readings (charge_type_id, unit_id, period, prev_reading, curr_reading)"
                               " VALUES (?,?,?,?,?)", (m["id"], u["id"], period, prev, curr))
                    last[(m["id"], u["id"])] = curr
            db.commit()
            billing.generate_invoices(db, period, get_settings())
            for inv in db.execute("SELECT * FROM invoices WHERE period=?", (period,)).fetchall():
                if rnd.random() < 0.8:
                    paid_at = f"{period}-{rnd.randint(2, 14):02d}"
                    receipt_no = billing.next_number(db, "payments", "receipt_no", paid_at)
                    db.execute("INSERT INTO payments (invoice_id, receipt_no, paid_at, amount, method) VALUES (?,?,?,?,?)",
                               (inv["id"], receipt_no, paid_at, inv["total"],
                                rnd.choice(["โอนเงิน", "เงินสด", "พร้อมเพย์"])))
                    billing.refresh_invoice_status(db, inv["id"])
        db.commit()
        print(f"สร้างข้อมูลตัวอย่างแล้ว: {len(units)} ห้อง, งวด {', '.join(periods[:-1])}")
        print(f"งวดปัจจุบัน {current} ยังไม่ได้จดมิเตอร์ — ลองจดมิเตอร์และออกบิลได้เลย")
        print("ลูกบ้านตัวอย่าง: room101 / room101")


if __name__ == "__main__":
    main()
