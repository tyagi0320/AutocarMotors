"""Daily Serper allowances shared by all bot conversations."""
from datetime import datetime, timezone

from .bookings import booking_db


def reserve_search(thread_id, turn_id, *, daily_limit, thread_limit):
    today = datetime.now(timezone.utc).date().isoformat()
    with booking_db(write=True) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS serper_usage (
            day TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            PRIMARY KEY (day, thread_id, turn_id)
        )""")
        db.execute("DELETE FROM serper_usage WHERE day < ?", (today,))
        total, thread_total = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(thread_id = ?), 0) FROM serper_usage WHERE day = ?",
            (thread_id, today),
        ).fetchone()
        if total >= daily_limit or thread_total >= thread_limit:
            return False
        return db.execute("INSERT OR IGNORE INTO serper_usage VALUES (?, ?, ?)",
                          (today, thread_id, turn_id)).rowcount == 1
