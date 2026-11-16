"""Booking approval and recovery through the FastAPI endpoints."""
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from ai import agent
from backend import api, mcp_server
from tests.support import AgentTestCase


class BookingFlowTests(AgentTestCase):
    def test_tool_loop_pending_restart_and_confirmation(self):
        self.steps.extend([
            ("Which cars", ("get_product_info", {})),
            ("Horizon EV", "We have the Urban Lite, Trailview, and Horizon EV."),
            ("family", "Trailview is a good family choice."),
            ("3 PM", ("check_slots", {"date": self.visit["date"]})),
            ('"15:00"', "3 PM is available. What is your name and email?"),
            ("priya@example.com", ("propose_booking", self.visit)),
        ])
        with TestClient(api.app) as client:
            self.assertIn("Trailview", self.chat(client, "Which cars do you have?")["message"])
            self.assertIn("family", self.chat(client, "Which is best for my family?")["message"])
            self.assertIn("unavailable", self.chat(client, "Search online for some reviews.")["message"])
            slots = self.chat(client, f"Can I see the Trailview on {self.visit['date']} at 3 PM?")
            self.assertIn("3 PM is available", slots["message"])
            state = self.chat(client, "Priya Rao, priya@example.com")
            self.assertEqual(state["status"], "waiting_for_approval")
            approval = state["approval"]
            self.assertEqual(approval["date"], self.visit["date"])
            self.assertEqual(self.saved_bookings(), [])
            self.assertEqual(self.chat(client, "yes")["approval"], approval)
            self.assertEqual(self.chat(client, thread_id=str(uuid4()))["messages"], [])

        with TestClient(api.app) as client:
            self.assertEqual(self.chat(client)["approval"], approval)
            confirmed = self.resume(client, approval, "confirm")
            self.assertEqual(confirmed.status_code, 200, confirmed.text)
            self.assertIn("is booked", confirmed.json()["message"])
            self.assertIn("calendar invitation are queued", confirmed.json()["message"])
            self.assertEqual(self.resume(client, approval, "confirm").status_code, 409)
            saved = self.saved_bookings()
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["booking_id"], approval["booking_id"])
            self.assertIn("Which cars", self.chat(client)["messages"][0]["content"])
        self.assertFalse(self.steps)

    def test_recover_when_mcp_response_is_lost(self):
        self.steps.append(("Book", ("propose_booking", self.visit)))
        lost = {"create_booking"}
        call_mcp = agent.call_mcp

        async def lose_first_response(tool, args):
            result = await call_mcp(tool, args)
            if tool.name in lost:
                lost.remove(tool.name)
                raise RuntimeError("Simulated interruption after the MCP write")
            return result

        with TestClient(api.app) as client, patch.object(agent, "call_mcp", lose_first_response):
            approval = self.chat(client, "Book a visit")["approval"]
            self.assertEqual(self.resume(client, approval, "confirm").status_code, 503)
            self.assertIn("is booked", self.chat(client)["message"])
            saved = self.saved_bookings()
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["booking_id"], approval["booking_id"])
            self.assertFalse(lost)

    def test_modify_reject_and_approval_rechecks_availability(self):
        updated = {**self.visit, "time": "17:00"}
        self.steps.extend([
            ("Book", ("propose_booking", self.visit)),
            ("5 PM", ("propose_booking", updated)),
            ("Book a different slot", ("propose_booking", self.visit)),
        ])
        with TestClient(api.app) as client:
            first = self.chat(client, "Book a visit")["approval"]
            changed = self.resume(client, first, "modify").json()
            self.assertIn("change", changed["message"])
            self.assertEqual(self.saved_bookings(), [])
            second = self.chat(client, "Move it to 5 PM")["approval"]
            self.assertNotEqual(first["booking_id"], second["booking_id"])
            self.assertEqual(second["time"], "17:00")
            self.assertEqual(self.resume(client, first, "confirm").status_code, 409)
            self.assertIn("discarded", self.resume(client, second, "reject").json()["message"])
            self.assertEqual(self.saved_bookings(), [])

            third = self.chat(client, "Book a different slot")["approval"]
            self.assertTrue(mcp_server.create_booking(**self.visit)["ok"])
            rejected = self.resume(client, third, "confirm").json()
            self.assertIn("no longer available", rejected["message"])
            self.assertEqual(len(self.saved_bookings()), 1)

    def test_lookup_reschedule_and_cancel_existing_booking(self):
        original = mcp_server.create_booking(**self.visit)["booking"]
        identity = {"booking_id": original["booking_id"], "email": original["email"]}
        self.steps.extend([
            ("Wrong email", ("get_booking", {**identity, "email": "wrong@example.com"})),
            ("No booking matches", "No booking matches that ID and email."),
            ("Find my booking", ("get_booking", identity)),
            ('"15:00"', "Your Trailview visit is at 15:00."),
            ("Move it", ("propose_reschedule", {**identity, "date": original["date"], "time": "17:00"})),
            ("Cancel it", ("propose_cancellation", identity)),
            ("Cancel after all", ("propose_cancellation", identity)),
            ("Look up the status", ("get_booking", identity)),
            ('"cancelled"', "Your visit is cancelled."),
        ])
        with TestClient(api.app) as client:
            self.assertIn("No booking matches", self.chat(client, "Wrong email")["message"])
            self.assertIn("15:00", self.chat(client, "Find my booking")["message"])
            moved = self.chat(client, "Move it to 5 PM")["approval"]
            self.assertEqual((moved["operation"], moved["previous_time"], moved["time"]),
                             ("reschedule", "15:00", "17:00"))
            self.assertNotEqual(moved["approval_id"], original["booking_id"])
            self.assertEqual(mcp_server.get_booking(**identity)["booking"]["time"], "15:00")

        with TestClient(api.app) as client:
            self.assertEqual(self.chat(client)["approval"], moved)
            self.assertEqual(self.resume(client, original, "confirm").status_code, 409)
            self.assertIn("rescheduled", self.resume(client, moved, "confirm").json()["message"])
            self.assertIn("15:00", mcp_server.check_slots(original["date"])["slots"])
            self.assertNotIn("17:00", mcp_server.check_slots(original["date"])["slots"])
            cancelled = self.chat(client, "Cancel it")["approval"]
            self.assertEqual(cancelled["operation"], "cancel")
            self.assertIn("discarded", self.resume(client, cancelled, "reject").json()["message"])
            self.assertTrue(mcp_server.get_booking(**identity)["can_change"])
            cancelled = self.chat(client, "Cancel after all")["approval"]
            self.assertIn("cancelled", self.resume(client, cancelled, "confirm").json()["message"])
            self.assertIn("17:00", mcp_server.check_slots(original["date"])["slots"])
            self.assertIn("cancelled", self.chat(client, "Look up the status")["message"])
            self.assertEqual(len(self.saved_bookings()), 1)
        self.assertFalse(self.steps)
