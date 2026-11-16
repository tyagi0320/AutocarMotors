"""Telegram booking and approval callbacks using a mocked Bot API."""
import json

import httpx
from fastapi.testclient import TestClient

from backend import api
from frontend.telegram_bot import TelegramBot
from tests.support import AgentTestCase


class TelegramTests(AgentTestCase):
    def test_telegram_intro_lists_five_capabilities_without_calling_agent(self):
        requests = []

        def route(request):
            self.assertEqual(request.url.host, "api.telegram.org")
            requests.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
            return httpx.Response(200, json={"ok": True, "result": True})

        with httpx.Client(transport=httpx.MockTransport(route)) as client:
            bot = TelegramBot(client, "123:mock", "http://api.local", self.directory / "telegram.sqlite")
            bot.configure_description()
            for update_id, command in enumerate(("/start", "/help"), start=1):
                bot.handle({"update_id": update_id, "message": {
                    "message_id": update_id, "text": command, "chat": {"id": 100, "type": "private"},
                }})
        self.assertEqual([method for method, _ in requests], ["setMyDescription", "sendMessage", "sendMessage"])
        description = requests[0][1]["description"]
        self.assertLessEqual(len(description), 512)
        self.assertEqual(description.count("• "), 5)
        for _, payload in requests[1:]:
            self.assertEqual(payload["parse_mode"], "HTML")
            self.assertEqual(payload["text"].count("• "), 5)
            self.assertIn("Which cars do you sell?", payload["text"])

    def test_telegram_approval(self):
        self.steps.extend([
            ("Trailview", "Would you like to schedule a visit?"),
            ("Yes please", "What day works for you?"),
            (self.visit["date"], ("check_slots", {"date": self.visit["date"]})),
            ('"15:00"', "Please share your name and email."),
            ("3 PM", "3 PM is available. What name should I put on the booking?"),
            ("Priya Rao", "What is your email address?"),
            ("priya@example.com", ("propose_booking", self.visit)),
        ])
        sent = []
        sessions = self.directory / "telegram.sqlite"
        with TestClient(api.app) as client:
            def route(request):
                payload = json.loads(request.content)
                if request.url.host == "api.telegram.org":
                    method = request.url.path.rsplit("/", 1)[-1]
                    if method == "sendMessage":
                        sent.append((method, payload))
                    return httpx.Response(200, json={"ok": True, "result": True})
                reply = client.post(request.url.path, json=payload)
                return httpx.Response(reply.status_code, json=reply.json())

            def send_text(text, update_id, chat_id=100, chat_type="private"):
                bot.handle({"update_id": update_id, "message": {
                    "message_id": update_id, "chat": {"id": chat_id, "type": chat_type}, "text": text,
                }})

            def press(callback_data, update_id, sender=100):
                bot.handle({"update_id": update_id, "callback_query": {
                    "id": str(update_id), "data": callback_data, "from": {"id": sender},
                    "message": {"message_id": 500, "chat": {"type": "private", "id": 100}},
                }})

            with httpx.Client(transport=httpx.MockTransport(route)) as transport:
                bot = TelegramBot(transport, "123:mock", "http://api.local", sessions)
                send_text("Book a visit", 1, chat_type="group")
                self.assertEqual(sent, [])
                send_text("Tell me about the Trailview", 2)
                send_text("Yes please", 21)
                self.assertIn("What day", sent[-1][1]["text"])
                send_text(self.visit["date"], 22)
                self.assertIn("15:00", sent[-1][1]["text"])
                self.assertNotIn("email", sent[-1][1]["text"])
                send_text("3 PM", 23)
                self.assertIn("What name", sent[-1][1]["text"])
                send_text("Priya Rao", 24)
                self.assertIn("email address", sent[-1][1]["text"])
                send_text("priya@example.com", 25)
                card = sent[-1][1]
                buttons = card["reply_markup"]["inline_keyboard"][0]
                self.assertEqual([button["text"] for button in buttons], ["Confirm", "Reject", "Modify"])
                self.assertIn(self.visit["email"], card["text"])
                confirm = buttons[0]["callback_data"]
                press(confirm, 3, sender=999)
                self.assertEqual(self.saved_bookings(), [])

                original_thread = bot.thread_id(100)
                bot = TelegramBot(transport, "123:mock", "http://api.local", sessions)
                self.assertEqual(bot.thread_id(100), original_thread)
                send_text("/status", 4)
                self.assertEqual(sent[-1][1]["reply_markup"], card["reply_markup"])
                press(confirm, 5)
                self.assertIn("is booked", sent[-1][1]["text"])
                press(confirm, 6)
                send_text("priya@example.com", 25)
                self.assertEqual(len(self.saved_bookings()), 1)
                self.assertIn("is booked", sent[-1][1]["text"])

                booking = self.saved_bookings()[0]
                identity = {"booking_id": booking["booking_id"], "email": booking["email"]}
                self.steps.extend([
                    ("Move", ("propose_reschedule", {**identity, "date": booking["date"], "time": "17:00"})),
                    ("Cancel", ("propose_cancellation", identity)),
                ])
                send_text("Move my booking to 5 PM", 7)
                self.assertIn("Original slot", sent[-1][1]["text"])
                press(sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], 8)
                self.assertIn("rescheduled", sent[-1][1]["text"])
                send_text("Cancel my booking", 9)
                buttons = sent[-1][1]["reply_markup"]["inline_keyboard"][0]
                self.assertEqual([button["text"] for button in buttons], ["Confirm", "Reject"])
                press(buttons[0]["callback_data"], 10)
                self.assertIn("cancelled", sent[-1][1]["text"])
                send_text("/new", 11)
                reset_thread = bot.thread_id(100)
                self.assertNotEqual(reset_thread, original_thread)
                send_text("/new", 11)
                self.assertEqual(bot.thread_id(100), reset_thread)
