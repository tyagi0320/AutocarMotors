"""Deliver pending booking notifications outside the approval request."""
import asyncio
import logging
from time import time

from ai.tools import call_mcp
from database.bookings import booking_db

logger = logging.getLogger(__name__)


async def deliver_email_jobs(tool):
    while True:
        try:
            with booking_db() as db:
                booking = db.execute(
                    "SELECT booking_id, email FROM bookings WHERE email_status = 'pending' ORDER BY rowid LIMIT 1"
                ).fetchone()
            if booking:
                await call_mcp(tool, {
                    "email": booking["email"], "booking_details": {"booking_id": booking["booking_id"]},
                })
                continue
        except Exception:
            logger.warning("Booking email delivery worker could not process pending jobs.")
        await asyncio.sleep(0.5)


async def deliver_calendar_jobs(tool):
    while True:
        try:
            with booking_db() as db:
                job = db.execute(
                    """SELECT booking_id, revision, calendar_attempts FROM bookings
                       WHERE calendar_status = 'pending' AND calendar_retry_at <= ? ORDER BY rowid LIMIT 1""",
                    (int(time()),),
                ).fetchone()
            if job:
                await deliver_calendar_job(tool, job)
                continue
        except Exception:
            logger.warning("Calendar delivery worker could not process pending jobs.")
        await asyncio.sleep(0.5)


async def deliver_calendar_job(tool, job):
    try:
        result = await call_mcp(tool, {"booking_id": job["booking_id"], "revision": job["revision"]})
        status = result["status"]
    except Exception:
        status = "retry"
    attempts = job["calendar_attempts"] + 1
    retry_at = 0
    if status == "retry" and attempts < 5:
        status, retry_at = "pending", int(time()) + 5 * 2 ** (attempts - 1)
    elif status == "retry":
        status = "failed"
    with booking_db(write=True) as db:
        # A reschedule or cancellation that arrived during the request gets its own sync.
        db.execute(
            """UPDATE bookings SET calendar_status = ?, calendar_attempts = ?, calendar_retry_at = ?
               WHERE booking_id = ? AND revision = ?""",
            (status, attempts, retry_at, job["booking_id"], job["revision"]),
        )
