"""Opt-in LangSmith tracing with telemetry-only payloads."""
import logging
import os

from langsmith import Client, agent_address

logger = logging.getLogger(__name__)
METRIC_FIELDS = {"usage_metadata", "ls_provider", "ls_model_name", "ls_model_type"}


def redact_trace(data):
    if "error" in data:
        return {"error": "Run failed. See the application logs for details."}
    return {key: value for key, value in data.items() if key in METRIC_FIELDS}


def trace_destination():
    agent_id = os.getenv("LANGSMITH_AGENT_ID", "").strip()
    if agent_id:
        environment = os.getenv("LANGSMITH_AGENT_ENVIRONMENT", "").strip() or "local"
        return {"address": agent_address(agent_id, environment)}
    return {"project_name": os.getenv("LANGSMITH_PROJECT", "").strip() or "autocare-motors"}


def create_tracing_client():
    if os.getenv("LANGSMITH_TRACING", "").strip().lower() != "true":
        return None
    key = os.getenv("LANGSMITH_API_KEY", "").strip()
    if not key:
        logger.warning("LangSmith tracing is disabled because LANGSMITH_API_KEY is missing.")
        return None
    return Client(
        api_key=key,
        hide_inputs=True,
        hide_outputs=True,
        anonymizer=redact_trace,
        omit_traced_runtime_info=True,
    )
