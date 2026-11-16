"""Verify LangSmith receives timings and usage without conversation data."""
import json
import os
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.tracers.langchain import wait_for_all_tracers
from langsmith import Client
from requests import Response, Session

from backend import api
from core.observability import redact_trace, trace_destination
from tests.support import AgentTestCase


class ObservabilityTests(AgentTestCase):
    def test_project_remains_available_without_an_agent_id(self):
        with patch.dict(os.environ, {"LANGSMITH_AGENT_ID": "", "LANGSMITH_PROJECT": "test-project"}):
            self.assertEqual(trace_destination(), {"project_name": "test-project"})

    def test_chat_exports_redacted_spans_and_token_counts(self):
        customer_message = "What is the price? My email is private@example.com."
        self.steps.extend([
            (customer_message, ("get_product_info", {})),
            ("Trailview", "The Trailview starts at ₹13,99,000."),
        ])
        payloads = []

        def record(method, url, **kwargs):
            if method in ("POST", "PATCH") and "/runs" in url:
                payloads.append(json.loads(kwargs["data"]))
            response = Response()
            response.status_code = 200
            response._content = b"{}"
            return response

        with Session() as session, patch.object(session, "request", side_effect=record):
            tracer = Client(
                api_key="test-key", api_url="https://langsmith.example", session=session,
                auto_batch_tracing=False, hide_inputs=True, hide_outputs=True, anonymizer=redact_trace,
                omit_traced_runtime_info=True,
            )
            destination_env = {"LANGSMITH_AGENT_ID": "test-agent", "LANGSMITH_AGENT_ENVIRONMENT": "local",
                               "LANGSMITH_PROJECT": "should-not-be-used"}
            with patch.dict(os.environ, destination_env), \
                    patch.object(api, "create_tracing_client", return_value=tracer), TestClient(api.app) as client:
                answer = self.chat(client, customer_message)
                self.assertIn("Trailview", answer["message"])
                wait_for_all_tracers()

        self.assertTrue(any(run.get("name") == "AutoCare chat" and run.get("start_time") for run in payloads))
        self.assertTrue(any(run.get("address") == "lrn:agents/test-agent/environments/local" for run in payloads))
        self.assertFalse(any(run.get("session_name") == "should-not-be-used" for run in payloads))
        self.assertTrue(any(run.get("name") == "get_product_info" for run in payloads))
        metadata = [run.get("extra", {}).get("metadata", {}) for run in payloads if run.get("extra")]
        self.assertEqual(
            [item["usage_metadata"]["total_tokens"] for item in metadata if "usage_metadata" in item],
            [120, 120],
        )
        serialized = json.dumps(payloads)
        for secret in (customer_message, "private@example.com", self.thread, answer["message"]):
            self.assertNotIn(secret, serialized)
