"""Standalone Streamlit dashboard for dealership records and inventory."""
import sqlite3
from datetime import date

import streamlit as st

from core.config import load_environment
from database.bookings import booking_db, list_bookings, list_customers
from database.cars import add_car, list_cars, remove_car, update_car

load_environment()

st.set_page_config(page_title="AutoCare Motors Admin", layout="wide")
st.title("AutoCare Motors Admin")

TEXT_FIELDS = {
    "make": "Make", "model": "Model", "body_style": "Body style",
    "fuel_type": "Fuel type", "transmission": "Transmission",
}
NUMBER_FIELDS = {
    "model_year": ("Model year", 1886, date.today().year),
    "seats": ("Seats", 1, 5),
    "power_hp": ("Power (hp)", 1, 100),
    "boot_litres": ("Boot capacity (L)", 0, 300),
    "price_inr": ("Price (₹)", 1, 750000),
}


def car_fields(initial):
    left, right = st.columns(2)
    values = {
        key: left.text_input(label, value=initial.get(key, ""), max_chars=100).strip()
        for key, label in TEXT_FIELDS.items()
    }
    for key, (label, minimum, default) in NUMBER_FIELDS.items():
        values[key] = right.number_input(label, min_value=minimum, value=initial.get(key, default), step=1)
    return values


def save_car(values, car_id=None):
    if any(not values[field] for field in TEXT_FIELDS):
        st.error("Fill in all five text fields.")
        return
    try:
        with booking_db(write=True) as db:
            if car_id is None:
                add_car(db, values)
            elif not update_car(db, car_id, values):
                st.error("This car could not be found. Refresh the inventory and try again.")
                return
    except sqlite3.IntegrityError as exc:
        if exc.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE":
            st.error("A car with this make, model, and model year already exists.")
        else:
            st.error("The car details did not pass database validation.")
        return
    except sqlite3.Error:
        st.error("Could not save the car. Try again.")
        return
    st.session_state["admin_notice"] = "Car added." if car_id is None else "Car updated."
    st.rerun()


def delete_car(car_id):
    try:
        with booking_db(write=True) as db:
            if not remove_car(db, car_id):
                st.error("This car could not be found. Refresh the inventory and try again.")
                return
    except sqlite3.Error:
        st.error("Could not remove the car. Try again.")
        return
    st.session_state["admin_notice"] = "Car removed."
    st.rerun()


try:
    with booking_db() as db:
        customers = list_customers(db)
        bookings = list_bookings(db)
        cars = list_cars(db)
except (sqlite3.Error, OSError, RuntimeError, ValueError, KeyError):
    st.error("Could not open the dealership database. Check the database path and restart the app.")
    st.stop()

for column, (label, rows) in zip(st.columns(3), (
    ("Customers", customers), ("Bookings", bookings), ("Cars", cars),
)):
    column.metric(label, len(rows))

bookings_tab, customers_tab, cars_tab = st.tabs(["Bookings", "Customers", "Cars"])

with bookings_tab:
    if bookings:
        st.dataframe(bookings, hide_index=True)
    else:
        st.info("No bookings yet.")

with customers_tab:
    if customers:
        st.dataframe(customers, hide_index=True)
    else:
        st.info("No customers yet.")

with cars_tab:
    if notice := st.session_state.pop("admin_notice", None):
        st.success(notice)
    if cars:
        st.dataframe(cars, hide_index=True)
    else:
        st.info("No cars in the inventory.")

    with st.expander("Add a car"):
        with st.form("add_car", clear_on_submit=False):
            new_car = car_fields({})
            if st.form_submit_button("Add car", type="primary"):
                save_car(new_car)

    if cars:
        st.subheader("Manage a car")
        cars_by_id = {car["car_id"]: car for car in cars}

        def car_label(car_id):
            car = cars_by_id[car_id]
            return f"{car['make']} {car['model']} ({car['model_year']})"

        selected = st.selectbox("Select a car", list(cars_by_id), format_func=car_label)
        with st.form(f"edit_car_{selected}"):
            changed_car = car_fields(cars_by_id[selected])
            if st.form_submit_button("Save changes", type="primary"):
                save_car(changed_car, selected)
            if st.form_submit_button("Remove car"):
                delete_car(selected)
