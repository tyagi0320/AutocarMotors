"""Background confirmation delivery, SMTP, and Google Calendar."""
import asyncio
import json
import os
import smtplib
from datetime import timedelta
from threading import Event
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from backend import api, booking_delivery, calendar_mcp, mcp_server
from tests.support import AgentTestCase, BookingTestCase


class BackgroundDeliveryTests(AgentTestCase):
    def test_confirmation_returns_while_delivery_is_blocked(self):
        self.steps.append(("Book", ("propose_booking", self.visit)))
        started = {name: Event() for name in ("send_email", "sync_calendar_booking")}
        release = Event()

        async def slow_delivery(tool, args):
            started[tool.name].set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return {"status": "unconfigured"}

        with patch.object(booking_delivery, "call_mcp", slow_delivery), TestClient(api.app) as client:
            approval = self.chat(client, "Book a visit")["approval"]
            self.assertFalse(any(event.is_set() for event in started.values()))
            try:
                result = self.resume(client, approval, "confirm")
                self.assertIn("is booked", result.json()["message"])
                self.assertTrue(all(event.wait(3) for event in started.values()))
                booking = self.saved_bookings()[0]
                self.assertEqual(booking["calendar_status"], "pending")
                self.assertEqual(booking["email_status"], "pending")
            finally:
                release.set()


class DeliveryProviderTests(BookingTestCase):
    def test_calendar_invitation_updates_and_cancels_same_event(self):
        booking = mcp_server.create_booking(**self.visit)["booking"]
        env = {f"GOOGLE_CALENDAR_{field}": "test" for field in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN")}
        remote = {}
        writes = []

        def calendar_request(request):
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "example"})
            if request.method == "GET":
                return httpx.Response(200, json=remote) if remote else httpx.Response(404)
            self.assertEqual(request.url.params["sendUpdates"], "all")
            payload = json.loads(request.content) if request.content else {}
            writes.append((request.method, request.url.path, payload))
            if request.method == "DELETE":
                remote.clear()
                return httpx.Response(204)
            remote.update(payload)
            return httpx.Response(200, json=remote)

        identity = {key: booking[key] for key in ("booking_id", "email")}
        client_factory = httpx.Client
        with patch.dict(os.environ, env), patch.object(
            calendar_mcp.httpx, "Client",
            side_effect=lambda **kwargs: client_factory(transport=httpx.MockTransport(calendar_request), **kwargs),
        ):
            self.assertEqual(calendar_mcp.sync_calendar_booking(booking["booking_id"], 0)["status"], "synced")
            self.assertEqual(calendar_mcp.sync_calendar_booking(booking["booking_id"], 0)["status"], "synced")
            self.assertTrue(mcp_server.reschedule_booking(
                **identity, date=self.visit["date"], time="17:00", expected_revision=0, change_id=str(uuid4())
            )["ok"])
            self.assertEqual(calendar_mcp.sync_calendar_booking(booking["booking_id"], 1)["status"], "synced")
            self.assertTrue(mcp_server.cancel_booking(**identity, expected_revision=1, change_id=str(uuid4()))["ok"])
            self.assertEqual(calendar_mcp.sync_calendar_booking(booking["booking_id"], 2)["status"], "cancelled")
        self.assertEqual([item[0] for item in writes], ["POST", "PATCH", "DELETE"])
        event = writes[0][2]
        self.assertEqual(event["attendees"], [{"email": booking["email"]}])
        self.assertIn("15:00:00+05:30", event["start"]["dateTime"])
        self.assertIn("15:45:00+05:30", event["end"]["dateTime"])
        self.assertIn("17:00:00+05:30", writes[1][2]["start"]["dateTime"])
        self.assertTrue(all(path.endswith("/" + event["id"]) for _, path, _ in writes[1:]))

    def test_email_outcomes_and_duplicate_writes(self):
        booking_id = str(uuid4())
        booking = mcp_server.create_booking(**self.visit, booking_id=booking_id)["booking"]
        self.assertEqual(mcp_server.create_booking(**self.visit, booking_id=booking_id)["booking"], booking)
        self.assertFalse(mcp_server.create_booking(**self.visit)["ok"])
        self.assertFalse(mcp_server.check_slots("2024-02-31")["ok"])
        self.assertFalse(mcp_server.check_slots("2000-01-01")["ok"])
        sunday = mcp_server.calendar_date.fromisoformat(self.visit["date"])
        sunday += timedelta(days=(6 - sunday.weekday()) % 7)
        self.assertEqual(mcp_server.check_slots(sunday.isoformat())["slots"], [])

        with patch.dict(os.environ, {"SMTP_HOST": "smtp.example.com", "SMTP_FROM_EMAIL": "visits@example.com"}):
            with patch("backend.mcp_server.smtplib.SMTP") as smtp:
                deliver = smtp.return_value.send_message
                for _ in range(2):
                    self.assertEqual(mcp_server.send_email(booking["email"], booking), {"status": "sent"})
                deliver.assert_called_once()
                smtp.return_value.starttls.assert_called_once()
                self.assertIn(booking_id, deliver.call_args.args[0].get_content())

            second = mcp_server.create_booking(**{**self.visit, "time": "17:00"})["booking"]
            with patch("backend.mcp_server.smtplib.SMTP", side_effect=smtplib.SMTPConnectError(421, "unavailable")):
                self.assertEqual(mcp_server.send_email(second["email"], second), {"status": "failed"})
            self.assertEqual(len(self.saved_bookings()), 2)
