"""SQLite booking rules, inventory changes, and migrations."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from backend import mcp_server
from database.bookings import booking_db
from tests.support import BookingTestCase


class BookingStorageTests(BookingTestCase):
    def test_changes_recheck_slot_and_guard_against_stale_or_replayed_approval(self):
        original = mcp_server.create_booking(**self.visit)["booking"]
        identity = {"booking_id": original["booking_id"], "email": original["email"]}
        move = {**identity, "date": original["date"], "time": "17:00",
                "expected_revision": 0, "change_id": str(uuid4())}
        competing = mcp_server.create_booking(**{**self.visit, "time": "17:00"})["booking"]
        self.assertFalse(mcp_server.reschedule_booking(**move)["ok"])
        self.assertEqual(mcp_server.get_booking(**identity)["booking"]["time"], "15:00")
        self.assertTrue(mcp_server.cancel_booking(competing["booking_id"], competing["email"], 0, str(uuid4()))["ok"])
        self.assertTrue(mcp_server.reschedule_booking(**move)["ok"])
        self.assertEqual(mcp_server.reschedule_booking(**move)["booking"]["revision"], 1)
        self.assertFalse(mcp_server.cancel_booking(**identity, expected_revision=0, change_id=str(uuid4()))["ok"])
        cancel = {**identity, "expected_revision": 1, "change_id": str(uuid4())}
        self.assertTrue(mcp_server.cancel_booking(**cancel)["ok"])
        self.assertEqual(mcp_server.cancel_booking(**cancel)["booking"]["revision"], 2)
        self.assertFalse(mcp_server.reschedule_booking(**move)["ok"])

    def test_catalog_and_booking_model_follow_database_edits(self):
        from database.cars import add_car, remove_car
        from ai.tools import get_product_info

        with booking_db(write=True) as db:
            row = dict(db.execute("SELECT * FROM cars WHERE model = 'Trailview'").fetchone())
            remove_car(db, row["car_id"])
            add_car(db, {**row, "model": "Demo One", "price_inr": 1234567})
        cars = {car["model"]: car for car in get_product_info.invoke({})["cars"]}
        self.assertNotIn("Trailview", cars)
        self.assertEqual(cars["Demo One"]["price_inr"], 1234567)
        self.assertFalse(mcp_server.create_booking(**self.visit)["ok"])
        self.assertTrue(mcp_server.create_booking(**{**self.visit, "car_model": "Demo One"})["ok"])

    def test_repeat_bookings_share_customer_in_sqlite(self):
        first = mcp_server.create_booking(**self.visit)["booking"]
        second = mcp_server.create_booking(
            **{**self.visit, "email": self.visit["email"].upper(), "time": "17:00"}
        )["booking"]
        self.assertNotEqual(first["booking_id"], second["booking_id"])
        self.assertEqual(first["customer_id"], second["customer_id"])
        with booking_db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM customers").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_upgrade_existing_database_and_seed_cars_once(self):
        original = mcp_server.create_booking(**self.visit)["booking"]
        with booking_db() as db:
            db.execute("DROP TABLE cars")
            for column in ("calendar_status", "calendar_attempts", "calendar_retry_at"):
                db.execute(f"ALTER TABLE bookings DROP COLUMN {column}")
            db.execute("PRAGMA user_version = 1")
        for _ in range(2):
            with booking_db() as db:
                cars = db.execute("SELECT * FROM cars").fetchall()
                self.assertEqual(len(cars), 10)
                self.assertTrue(all(len(car) == 11 and None not in car for car in cars))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM customers").fetchone()[0], 1)
        self.assertEqual(self.saved_bookings(), [{**original, "calendar_status": "skipped"}])

    def test_concurrent_slot_requests_only_save_winning_customer(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            orders = [executor.submit(mcp_server.create_booking, **{**self.visit, "email": email})
                      for email in ("first@example.com", "second@example.com")]
        self.assertEqual(sum(order.result()["ok"] for order in orders), 1)
        with booking_db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM customers").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM bookings").fetchone()[0], 1)
