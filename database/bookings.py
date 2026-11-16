"""SQLite connections, migrations, and booking queries."""
import os
import sqlite3
from contextlib import closing, contextmanager

from core.config import DATA_DIR

from .cars import create_cars

SCHEMA_VERSION = 3

SCHEMA = (
    """CREATE TABLE customers (
        customer_id INTEGER PRIMARY KEY,
        email_key TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        email TEXT NOT NULL
    )""",
    """CREATE TABLE bookings (
        booking_id TEXT PRIMARY KEY COLLATE NOCASE,
        customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        date TEXT NOT NULL,
        time TEXT NOT NULL,
        car_model TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
        revision INTEGER NOT NULL CHECK (revision >= 0),
        last_change_id TEXT,
        email_status TEXT NOT NULL
    )""",
    """CREATE UNIQUE INDEX unique_reserved_slot ON bookings(date, time)
       WHERE status = 'confirmed'""",
    "CREATE INDEX bookings_by_customer ON bookings(customer_id)",
)


def insert_booking(db, booking):
    # Keep the booking's original contact details even if the customer books again.
    email_key = booking["email"].strip().casefold()
    db.execute(
        """INSERT INTO customers (email_key, name, email) VALUES (?, ?, ?)
           ON CONFLICT(email_key) DO UPDATE SET name = excluded.name, email = excluded.email""",
        (email_key, booking["name"], booking["email"]),
    )
    db.execute(
        """INSERT INTO bookings
           (booking_id, customer_id, name, email, date, time, car_model, status, revision,
            last_change_id, email_status)
           VALUES (?, (SELECT customer_id FROM customers WHERE email_key = ?), ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (booking["booking_id"], email_key, booking["name"], booking["email"], booking["date"],
         booking["time"], booking["car_model"], booking.get("status", "confirmed"),
         booking.get("revision", 0), booking.get("last_change_id"), booking["email_status"]),
    )


def initialize(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return

    with db:
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, SCHEMA_VERSION):
            raise RuntimeError(f"Unsupported booking database version {version}.")
        if version == 0:
            for statement in SCHEMA:
                db.execute(statement)
        if version < 2:
            create_cars(db)
        if version < SCHEMA_VERSION:
            db.execute("ALTER TABLE bookings ADD COLUMN calendar_status TEXT NOT NULL DEFAULT 'skipped'")
            db.execute("ALTER TABLE bookings ADD COLUMN calendar_attempts INTEGER NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE bookings ADD COLUMN calendar_retry_at INTEGER NOT NULL DEFAULT 0")
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def booking_db(*, write=False):
    path = os.getenv("BOOKINGS_DB_PATH") or DATA_DIR / "bookings.sqlite"
    with closing(sqlite3.connect(path, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        initialize(db)
        with db:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db


def find_booking(db, booking_id, email=None):
    row = db.execute("SELECT * FROM bookings WHERE booking_id = ?", (booking_id.strip(),)).fetchone()
    if row is None or (email is not None and row["email"].casefold() != email.strip().casefold()):
        return None
    return dict(row)


def list_customers(db):
    return [dict(row) for row in db.execute(
        "SELECT customer_id, name, email FROM customers ORDER BY name, customer_id"
    )]


def list_bookings(db):
    return [dict(row) for row in db.execute(
        """SELECT booking_id, customer_id, name, email, date, time, car_model, status, email_status, calendar_status
           FROM bookings ORDER BY date DESC, time DESC, booking_id"""
    )]
