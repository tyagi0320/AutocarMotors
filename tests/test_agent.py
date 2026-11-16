"""Agent guardrails, provider errors, catalog answers, and financing."""
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from groq import APIConnectionError, AuthenticationError, BadRequestError, RateLimitError

from ai import agent
from backend import api
from core.dealership import dealership_info
from database.bookings import booking_db
from tests.support import AgentTestCase


class ProviderErrorTests(unittest.TestCase):
    def test_provider_failures_have_distinct_safe_replies(self):
        request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        with self.assertLogs("ai.agent", level="WARNING") as captured:
            for error_type, code, reply in (
                (RateLimitError, 429, "usage limit"),
                (AuthenticationError, 401, "configuration issue"),
                (BadRequestError, 400, "rephrasing"),
            ):
                response = httpx.Response(code, request=request)
                error = error_type("private provider response", response=response, body={"code": "sample_error"})
                self.assertIn(reply, agent.provider_error_reply(error))
            self.assertIn("couldn't connect", agent.provider_error_reply(APIConnectionError(request=request)))
        self.assertIn("HTTP status 429", "\n".join(captured.output))
        self.assertNotIn("private provider response", "\n".join(captured.output))


class AgentTests(AgentTestCase):
    def test_blocked_booking_proposal_ends_turn_without_another_groq_call(self):
        self.steps.extend([
            ("Is 3 PM available", ("propose_booking", self.visit)),
            ("Book a visit", ("propose_booking", self.visit)),
        ])
        with TestClient(api.app) as client, patch.object(agent, "call_mcp", wraps=agent.call_mcp) as mcp:
            blocked = self.chat(client, "Is 3 PM available tomorrow?")
            self.assertIn("check showroom availability", blocked["message"])
            self.assertIsNone(blocked["approval"])
            mcp.assert_not_called()
            self.assertEqual(len(self.steps), 1)
            allowed = self.chat(client, "Book a visit")
            self.assertEqual(allowed["status"], "waiting_for_approval")
            self.assertEqual(self.saved_bookings(), [])
        self.assertFalse(self.steps)

    def test_emi_mcp_uses_fixed_rate_and_discount_is_referred(self):
        self.steps.extend([
            ("EMI", ("calculate_emi", {"loan_amount_inr": 500000, "tenure_months": 60})),
            ("10,258.27", "Your estimated EMI is ₹10,258.27 per month at 8.5%."),
        ])
        with TestClient(api.app) as client:
            self.assertIn("in person", self.chat(client, "Any discount?")["message"])
            self.assertIn("8.5%", self.chat(client, "EMI for ₹500,000 over five years?")["message"])
        self.assertFalse(self.steps)

    def test_scope_search_gates_and_contact_collection(self):
        self.steps.extend([
            ("What's the score", ("web_search", {})),
            ("did not request an external automotive search", "I can help with cars and bookings."),
            ("Tell me about", "Please share your name and email."),
            ("Book a visit", "Could I get your email address?"),
        ])
        with TestClient(api.app) as client, patch.object(agent, "call_mcp", wraps=agent.call_mcp) as mcp:
            self.assertIn("showroom bookings", self.chat(client, "Search for today's weather in Bengaluru")["message"])
            self.assertIn("help with cars", self.chat(client, "What's the score in the match?")["message"])
            self.assertNotIn("email", self.chat(client, "Tell me about the Trailview")["message"])
            self.assertIn("day and time", self.chat(client, "Book a visit")["message"])
            mcp.assert_not_called()
        self.assertFalse(self.steps)

    def test_explicit_car_search_sends_only_the_fixed_query(self):
        self.steps.extend([
            ("A review", "Here is a review."),
        ])
        with TestClient(api.app) as client, patch.object(agent, "call_mcp") as mcp:
            mcp.return_value = {"ok": True, "results": [{"title": "A review"}]}
            answer = self.chat(client, "Search online for electric car reviews. My email is priya@example.com.")
            self.assertIn("review", answer["message"])
            mcp.assert_awaited_once()
            self.assertEqual(mcp.await_args.args[1]["query"], "electric car reviews")
        self.assertFalse(self.steps)

    def test_catalog_followup_reads_the_latest_price(self):
        self.assertNotIn("cars", dealership_info())
        self.steps.extend([
            ("Trailview price", ("get_product_info", {})),
            ("1399000", "The Trailview is ₹13,99,000."),
            ("price now", ("get_product_info", {})),
            ("1499000", "The current price is ₹14,99,000."),
        ])
        with TestClient(api.app) as client:
            self.assertIn("13,99,000", self.chat(client, "What's the Trailview price?")["message"])
            with booking_db(write=True) as db:
                db.execute("UPDATE cars SET price_inr = 1499000 WHERE model = 'Trailview'")
            self.assertIn("14,99,000", self.chat(client, "What's the price now?")["message"])
        self.assertFalse(self.steps)
