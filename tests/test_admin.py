"""Streamlit admin list views and car inventory management."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

from backend import mcp_server
from database.bookings import booking_db
from tests.support import BookingTestCase


class AdminTests(BookingTestCase):
    def test_admin_views_and_car_management(self):
        original = mcp_server.create_booking(**self.visit)["booking"]
        ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "frontend" / "admin_app.py"), default_timeout=30).run()
        self.assertFalse(ui.exception)
        self.assertEqual([tab.label for tab in ui.tabs], ["Bookings", "Customers", "Cars"])
        self.assertEqual(ui.dataframe[0].value.iloc[0]["booking_id"], original["booking_id"])
        self.assertEqual(ui.dataframe[1].value.iloc[0]["email"], self.visit["email"])

        for index, value in enumerate(("Test Make", "Test Model", "SUV", "Electric", "Single-speed")):
            ui.text_input[index].set_value(value)
        next(button for button in ui.button if button.label == "Add car").click().run()
        self.assertEqual(ui.success[0].value, "Car added.")
        with booking_db() as db:
            car_id = db.execute("SELECT car_id FROM cars WHERE model = 'Test Model'").fetchone()[0]
        ui.selectbox[0].set_value(car_id).run()
        ui.text_input[6].set_value("Updated Model")
        next(button for button in ui.button if button.label == "Save changes").click().run()
        self.assertEqual(ui.success[0].value, "Car updated.")
        self.assertIn("Updated Model", ui.dataframe[2].value["model"].tolist())
        next(button for button in ui.button if button.label == "Remove car").click().run()
        self.assertEqual(ui.success[0].value, "Car removed.")
        self.assertEqual(len(ui.dataframe[2].value), 10)
        self.assertEqual(self.saved_bookings(), [original])
