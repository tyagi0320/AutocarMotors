"""Temporary databases and mock Groq fixtures shared by the tests."""
import json
import os
import unittest
from collections import deque
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

import httpx
from langchain_groq import ChatGroq

from ai.agent import build_graph
from backend import api
from core.dealership import dealership_info, showroom_now
from database.bookings import booking_db

MODEL = "openai/gpt-oss-120b"


def future_open_date():
    now = showroom_now(dealership_info())
    day = now.date() + timedelta(days=2)
    return (day + timedelta(days=1) if day.weekday() == 6 else day).isoformat()


class BookingTestCase(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.directory = directory
        self.enterContext(patch.dict(os.environ, {
            "BOOKINGS_DB_PATH": str(directory / "bookings.sqlite"),
            "CHECKPOINT_PATH": str(directory / "checkpoints.sqlite"),
            "SMTP_HOST": "",
            "SMTP_FROM_EMAIL": "",
            "SMTP_PORT": "587",
            "SMTP_USER": "",
            "SERPER_API_KEY": "",
            "LANGSMITH_TRACING": "false",
            "GOOGLE_CALENDAR_CLIENT_ID": "",
            "GOOGLE_CALENDAR_CLIENT_SECRET": "",
            "GOOGLE_CALENDAR_REFRESH_TOKEN": "",
        }))
        self.visit = {
            "name": "Priya Rao",
            "email": "priya@example.com",
            "date": future_open_date(),
            "time": "15:00",
            "car_model": "Trailview",
        }

    def saved_bookings(self):
        with booking_db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM bookings")]


class AgentTestCase(BookingTestCase):
    def setUp(self):
        super().setUp()
        self.thread = str(uuid4())
        self.steps = deque()
        self.enterContext(patch.object(api, "build_graph", self.graph_with_mock_groq))

    def graph_with_mock_groq(self, saver, tools):
        async def mock_groq(request):
            data = json.loads(request.content)
            self.assertEqual(data["model"], MODEL)
            expected_tools = {"get_product_info", "check_slots", "get_booking", "calculate_emi"}
            if "The customer is arranging or managing a visit: yes." in data["messages"][0]["content"]:
                expected_tools.update(("propose_booking", "propose_reschedule", "propose_cancellation"))
            self.assertEqual({item["function"]["name"] for item in data["tools"]}, expected_tools)
            self.assertFalse(data.get("parallel_tool_calls", False))
            previous, answer = self.steps.popleft()
            self.assertIn(previous, data["messages"][-1]["content"])
            if isinstance(answer, tuple):
                name, args = answer
                message = {"role": "assistant", "tool_calls": [{
                    "id": f"call_{uuid4().hex}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }]}
            else:
                message = {"role": "assistant", "content": answer}
            return httpx.Response(200, json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 1,
                "model": MODEL,
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                "choices": [{"index": 0, "message": message,
                             "finish_reason": "tool_calls" if isinstance(answer, tuple) else "stop"}],
            })

        model = ChatGroq(model=MODEL, api_key="test-key", max_retries=0,
                         http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(mock_groq)))
        return build_graph(saver, tools, model)

    def chat(self, client, message=None, thread_id=None):
        response = client.post("/chat", json={"thread_id": thread_id or self.thread, "message": message})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def resume(self, client, approval, decision):
        return client.post("/resume", json={"thread_id": self.thread,
                                          "approval_id": approval.get("approval_id", approval["booking_id"]),
                                          "decision": decision})
