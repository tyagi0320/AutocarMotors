"""Synchronize authorized showroom bookings to Google Calendar."""
import hashlib
import os
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP

from core.config import load_environment
from core.dealership import dealership_info
from database.bookings import booking_db, find_booking

server = FastMCP("Google Calendar")
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_URL = "https://www.googleapis.com/calendar/v3"


@server.tool(structured_output=True)
def sync_calendar_booking(booking_id: str, revision: int) -> dict[str, str]:
    """Create, move, or cancel a confirmed booking's Google Calendar event."""
    with booking_db() as db:
        booking = find_booking(db, booking_id)
    if not booking or booking["revision"] != revision or booking["calendar_status"] != "pending":
        return {"status": "skipped"}

    credentials = {field: os.getenv(f"GOOGLE_CALENDAR_{field.upper()}") for field in
                   ("client_id", "client_secret", "refresh_token")}
    if not all(credentials.values()):
        return {"status": "unconfigured"}
    calendar_id = quote(os.getenv("GOOGLE_CALENDAR_ID") or "primary", safe="")
    # Google accepts hexadecimal IDs. Reusing the ID prevents duplicate invites after a retry.
    event_id = hashlib.sha256(booking_id.encode()).hexdigest()
    events_url = f"{GOOGLE_CALENDAR_URL}/calendars/{calendar_id}/events"
    event_url = f"{events_url}/{event_id}"
    notifications = {"sendUpdates": "all"}

    try:
        with httpx.Client(timeout=10) as client:
            token = client.post(GOOGLE_TOKEN_URL, data={**credentials, "grant_type": "refresh_token"})
            token.raise_for_status()
            client.headers["Authorization"] = f"Bearer {token.json()['access_token']}"
            if booking["status"] == "cancelled":
                response = client.delete(event_url, params=notifications)
                if response.status_code not in (404, 410):
                    response.raise_for_status()
                return {"status": "cancelled"}

            info = dealership_info()
            tz = info["timezone"]
            start = datetime.fromisoformat(f"{booking['date']}T{booking['time']}").replace(tzinfo=ZoneInfo(tz))
            end = start + timedelta(minutes=info["visits"]["duration_minutes"])
            event = {
                "summary": f"AutoCare Motors showroom visit · {booking['car_model']}",
                "description": f"Booking ID: {booking_id}",
                "location": info["address"],
                "start": {"dateTime": start.isoformat(), "timeZone": tz},
                "end": {"dateTime": end.isoformat(), "timeZone": tz},
                "attendees": [{"email": booking["email"]}],
                "extendedProperties": {"private": {"booking_revision": str(revision)}},
            }

            current = client.get(event_url)
            if current.status_code == 404:
                response = client.post(events_url, params=notifications, json={"id": event_id, **event})
                if response.status_code == 409:
                    return {"status": "retry"}
                response.raise_for_status()
            else:
                current.raise_for_status()
                saved = current.json().get("extendedProperties", {}).get("private", {})
                if saved.get("booking_revision") != str(revision):
                    client.patch(event_url, params=notifications, json=event).raise_for_status()
        return {"status": "synced"}
    except httpx.RequestError:
        return {"status": "retry"}
    except httpx.HTTPStatusError as exc:
        return {"status": "retry" if exc.response.status_code >= 500 or exc.response.status_code == 429 else "failed"}
    except (ValueError, KeyError):
        return {"status": "failed"}


if __name__ == "__main__":
    load_environment()
    server.run(transport="stdio")
