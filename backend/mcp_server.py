"""Run the dealership or Serper MCP server over stdio."""
import asyncio
import os
import smtplib
import ssl
import sys
from datetime import date as calendar_date, datetime, time as clock_time
from email.message import EmailMessage
from typing import Any
from uuid import uuid4

import certifi
import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from backend.schemas import Visit
from core.config import load_environment
from core.dealership import dealership_info, showroom_now
from database.bookings import booking_db, find_booking, insert_booking
from database.cars import has_model
from database.search import reserve_search
from core.search_policy import APPROVED_QUERIES

booking_server = FastMCP("AutoCare Motors")
search_server = FastMCP("Serper Search")


def available_slots(date, db, info):
    try:
        day = calendar_date.fromisoformat(date)
        if date != day.isoformat():
            raise ValueError
    except ValueError:
        return {"ok": False, "message": "Use a valid date in YYYY-MM-DD format.", "slots": []}

    now = showroom_now(info)
    if day < now.date():
        return {"ok": False, "message": "That date is in the past.", "slots": []}

    occupied = {row["time"] for row in db.execute(
        "SELECT time FROM bookings WHERE date = ? AND status = 'confirmed'", (date,)
    )}
    day_name = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[day.weekday()]
    slots = []
    if day_name in info["opening_hours"]["days"]:
        slots = [
            slot for slot in info["visits"]["slots"]
            if slot not in occupied
            and datetime.combine(day, clock_time.fromisoformat(slot), now.tzinfo) > now
        ]
    return {"ok": True, "date": date, "timezone": info["timezone"], "slots": slots}


@booking_server.tool(structured_output=True)
def check_slots(date: str) -> dict[str, Any]:
    """List open future showroom slots on a local date formatted YYYY-MM-DD."""
    with booking_db() as db:
        return available_slots(date, db, dealership_info())


def booking_can_change(booking, info):
    now = showroom_now(info)
    start = datetime.fromisoformat(f"{booking['date']}T{booking['time']}").replace(tzinfo=now.tzinfo)
    return booking.get("status") != "cancelled" and start > now


@booking_server.tool(structured_output=True)
def get_booking(booking_id: str, email: str) -> dict[str, Any]:
    """Retrieve a showroom booking using its confirmation ID and booking email."""
    with booking_db() as db:
        booking = find_booking(db, booking_id, email)
    if booking is None:
        return {"ok": False, "message": "No booking matches that ID and email."}
    info = dealership_info()
    return {
        "ok": True,
        "booking": {
            **{key: booking[key] for key in ("booking_id", "name", "email", "date", "time", "car_model")},
            "status": booking.get("status", "confirmed"),
            "revision": booking.get("revision", 0),
        },
        "can_change": booking_can_change(booking, info),
        "calendar_status": booking["calendar_status"],
        "timezone": info["timezone"],
    }


@booking_server.tool(structured_output=True)
def create_booking(name: str, email: str, date: str, time: str, car_model: str,
                   booking_id: str = "") -> dict[str, Any]:
    """Reserve a slot after approval. Pass the same booking ID if retrying."""
    try:
        visit = Visit(name=name, email=email, date=date, time=time, car_model=car_model).model_dump()
    except ValidationError:
        return {"ok": False, "message": "Please check the name, email, date, and time."}

    info = dealership_info()
    booking_id = booking_id or str(uuid4())
    with booking_db(write=True) as db:
        booking = find_booking(db, booking_id)
        if booking is not None:
            if booking["status"] == "confirmed" and all(booking[field] == value for field, value in visit.items()):
                return {"ok": True, "booking": booking}
            return {"ok": False, "message": "This booking ID belongs to a different visit."}

        if car_model != "General showroom visit" and not has_model(db, car_model):
            return {"ok": False, "message": "Please choose a current AutoCare model or a general showroom visit."}
        availability = available_slots(date, db, info)
        if not availability["ok"]:
            return availability
        if time not in availability["slots"]:
            return {"ok": False, "message": "That slot is no longer available. Please choose another time."}

        insert_booking(db, {"booking_id": booking_id, **visit, "email_status": "pending"})
        db.execute("UPDATE bookings SET calendar_status = 'pending' WHERE booking_id = ?", (booking_id,))
        return {"ok": True, "booking": find_booking(db, booking_id)}


def change_booking(booking_id, email, expected_revision, change_id, new_slot=None):
    info = dealership_info()
    with booking_db(write=True) as db:
        booking = find_booking(db, booking_id, email)
        if booking is None:
            return {"ok": False, "message": "No booking matches that ID and email."}
        revision = booking.get("revision", 0)
        if booking.get("last_change_id") == change_id and revision == expected_revision + 1:
            return {"ok": True, "booking": booking}
        if revision != expected_revision:
            return {"ok": False, "message": "This booking changed after you reviewed it. Look it up again."}
        if not booking_can_change(booking, info):
            return {"ok": False, "message": "This booking is cancelled or has already started."}

        if new_slot is None:
            booking["status"] = "cancelled"
        else:
            date, time = new_slot
            if (date, time) == (booking["date"], booking["time"]):
                return {"ok": False, "message": "Your booking is already at that date and time."}
            availability = available_slots(date, db, info)
            if not availability["ok"]:
                return availability
            if time not in availability["slots"]:
                return {"ok": False, "message": "That slot is no longer available. Please choose another time."}
            booking.update(date=date, time=time)

        booking.update(revision=revision + 1, last_change_id=change_id)
        db.execute(
            """UPDATE bookings SET date = :date, time = :time, status = :status,
               revision = :revision, last_change_id = :last_change_id, calendar_status = 'pending',
               calendar_attempts = 0, calendar_retry_at = 0 WHERE booking_id = :booking_id""",
            booking,
        )
        return {"ok": True, "booking": booking}


@booking_server.tool(structured_output=True)
def reschedule_booking(booking_id: str, email: str, date: str, time: str,
                       expected_revision: int, change_id: str) -> dict[str, Any]:
    """Move a booking to a new slot after approval."""
    return change_booking(booking_id, email, expected_revision, change_id, (date, time))


@booking_server.tool(structured_output=True)
def cancel_booking(booking_id: str, email: str, expected_revision: int, change_id: str) -> dict[str, Any]:
    """Cancel a booking and release its slot after approval."""
    return change_booking(booking_id, email, expected_revision, change_id)


def smtp_tls_context():
    context = ssl.create_default_context()
    if not context.get_ca_certs():
        context.load_verify_locations(cafile=certifi.where())
    return context


def deliver_confirmation(booking, info):
    host = os.getenv("SMTP_HOST")
    sender = os.getenv("SMTP_FROM_EMAIL")
    if not host or not sender:
        return "unconfigured"

    port = int(os.getenv("SMTP_PORT") or 587)
    message = EmailMessage()
    message["Subject"] = "AutoCare Motors showroom visit confirmation"
    message["From"] = sender
    message["To"] = booking["email"]
    message.set_content(
        f"Hi {booking['name']},\n\nYour showroom visit is confirmed.\n"
        f"Date: {booking['date']} at {booking['time']} ({info['timezone']})\n"
        f"Vehicle: {booking['car_model']}\n"
        f"Duration: {info['visits']['duration_minutes']} minutes\n"
        f"Location: {info['address']}\n"
        f"Booking ID: {booking['booking_id']}\n\nAutoCare Motors"
    )

    context = smtp_tls_context()
    smtp = (smtplib.SMTP_SSL(host, port, timeout=10, context=context) if port == 465
            else smtplib.SMTP(host, port, timeout=10))
    try:
        if port != 465:
            smtp.starttls(context=context)
        if user := os.getenv("SMTP_USER"):
            smtp.login(user, os.getenv("SMTP_PASSWORD", ""))
        smtp.send_message(message)
    finally:
        smtp.close()
    return "sent"


def send_email(email: str, booking_details: dict) -> dict[str, str]:
    """Send one confirmation attempt to the saved email for a booking ID."""
    booking_id = booking_details.get("booking_id")
    if not isinstance(booking_id, str):
        return {"status": "failed"}
    with booking_db(write=True) as db:
        booking = find_booking(db, booking_id, email)
        if not booking:
            return {"status": "failed"}
        if booking["email_status"] != "pending":
            return {"status": booking["email_status"]}
        if booking["revision"]:
            db.execute("UPDATE bookings SET email_status = 'skipped' WHERE booking_id = ?", (booking_id,))
            return {"status": "skipped"}
        # A crash after SMTP accepts mail can leave "sending". Do not resend it.
        db.execute("UPDATE bookings SET email_status = 'sending' WHERE booking_id = ?", (booking_id,))

    try:
        status = deliver_confirmation(booking, dealership_info())
    except (OSError, smtplib.SMTPException, ValueError):
        status = "failed"

    with booking_db(write=True) as db:
        db.execute("UPDATE bookings SET email_status = ? WHERE booking_id = ?", (status, booking_id))
    return {"status": status}


@booking_server.tool(name="send_email", structured_output=True)
async def send_email_tool(email: str, booking_details: dict) -> dict[str, str]:
    """Send a saved booking's confirmation without delaying other booking calls."""
    return await asyncio.to_thread(send_email, email, booking_details)


@search_server.tool(structured_output=True)
def web_search(query: str, thread_id: str, turn_id: str) -> dict[str, Any]:
    """Run an approved automotive query within the daily Serper budget."""
    if query not in APPROVED_QUERIES:
        return {"ok": False, "message": "Only approved automotive searches are supported."}
    key = os.getenv("SERPER_API_KEY")
    if not key:
        return {"ok": False, "message": "Web search is unavailable because Serper is not configured."}

    try:
        daily_limit = int(os.getenv("SERPER_DAILY_LIMIT") or 20)
        thread_limit = int(os.getenv("SERPER_THREAD_DAILY_LIMIT") or 3)
        if not reserve_search(thread_id, turn_id, daily_limit=daily_limit, thread_limit=thread_limit):
            return {"ok": False, "message": "The search allowance is exhausted or this request was already searched."}
        response = httpx.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": key},
            json={"q": query, "num": 5},
            timeout=10,
        )
        response.raise_for_status()
        results = response.json().get("organic", [])[:5]
        return {
            "ok": True,
            "results": [{field: item.get(field, "") for field in ("title", "link", "snippet")}
                        for item in results],
        }
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            return {"ok": False, "message": "Web search is unavailable because Serper denied the request."}
        return {"ok": False, "message": "Web search is temporarily unavailable."}
    except (httpx.RequestError, ValueError, AttributeError, TypeError):
        return {"ok": False, "message": "Web search is temporarily unavailable."}


if __name__ == "__main__":
    load_environment()
    server = search_server if sys.argv[1:] == ["serper"] else booking_server
    if server is booking_server:
        with booking_db():
            pass
    server.run(transport="stdio")
