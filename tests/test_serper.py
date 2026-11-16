"""Serper request shaping and search quotas."""
import os
from unittest.mock import patch

import httpx

from backend import mcp_server
from core.search_policy import APPROVED_QUERIES, external_query
from tests.support import BookingTestCase


class SerperTests(BookingTestCase):
    def test_automotive_queries_cover_common_questions_without_forwarding_user_text(self):
        cases = {
            "Hyundai Creta vs Kia Seltos": "Hyundai Creta vs Kia Seltos comparison",
            "What's the latest Tata Nexon EV recall? My email is private@example.com.": "Tata Nexon EV recalls",
            "Search online for EV charging and range": "electric car charging and range",
            "Look up tyre maintenance for cars": "cars maintenance",
            "Latest car launches": "cars news",
            "Search for the weather for my car trip": None,
            "Search for your Trailview price": None,
            "Don't search online for car reviews": None,
        }
        for prompt, query in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(external_query(prompt), query)
                if query:
                    self.assertIn(query, APPROVED_QUERIES)

    def test_serper_payload_and_results(self):
        with patch.dict(os.environ, {"SERPER_API_KEY": "test-key", "SERPER_DAILY_LIMIT": "2",
                                    "SERPER_THREAD_DAILY_LIMIT": "1"}):
            search_response = httpx.Response(200, request=httpx.Request("POST", "https://google.serper.dev/search"),
                                             json={"organic": [{"title": "A review", "link": "https://example.com",
                                                                "snippet": "A family SUV review.", "position": 1}]})
            with patch("backend.mcp_server.httpx.post", return_value=search_response) as post:
                self.assertFalse(mcp_server.web_search("Bengaluru weather", "a", "1")["ok"])
                post.assert_not_called()
                result = mcp_server.web_search("compact SUV reviews", "a", "1")
                self.assertFalse(mcp_server.web_search("compact SUV reviews", "a", "2")["ok"])
                self.assertTrue(mcp_server.web_search("cars news", "b", "1")["ok"])
                self.assertFalse(mcp_server.web_search("cars news", "c", "1")["ok"])
            self.assertTrue(result["ok"])
            self.assertEqual(result["results"][0]["title"], "A review")
            self.assertEqual(post.call_args.args[0], "https://google.serper.dev/search")
            self.assertEqual(post.call_args_list[0].kwargs["json"]["q"], "compact SUV reviews")
            self.assertEqual(post.call_count, 2)
