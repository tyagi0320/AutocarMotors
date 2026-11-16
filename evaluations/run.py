"""Run synthetic agent scenarios locally or publish a LangSmith experiment."""
import argparse
import asyncio
import hashlib
import json
import os
from datetime import timedelta
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from langsmith import Client, tracing_context

from ai.agent import build_graph
from ai.tools import load_tools
from backend.api import response
from core.config import load_environment
from core.dealership import dealership_info, showroom_now
from database.bookings import booking_db, insert_booking

from .cases import CASES
from .metrics import code_checks, judge_checks


class ToolRecorder(BaseCallbackHandler):
    def __init__(self):
        self.calls = []
        self.evidence = []

    def on_tool_start(self, serialized, input_str, *, name=None, **kwargs):
        self.calls.append(name or (serialized or {}).get("name", "unknown"))

    def on_tool_end(self, output, *, name=None, **kwargs):
        if isinstance(output, ToolMessage):
            data = output.artifact or output.content
            self.evidence.append({"tool": name or output.name, "result": data})
        else:
            self.evidence.append({"tool": name, "result": output})


def visit_date():
    date = showroom_now(dealership_info()).date() + timedelta(days=2)
    if date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


def booking_count():
    with booking_db() as db:
        return db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]


async def run_scenario(inputs, checkpoint_path):
    date = visit_date()
    if inputs.get("seed_private_booking"):
        with booking_db(write=True) as db:
            insert_booking(db, {"booking_id": str(uuid4()), "name": "Sensitive Test Customer",
                                "email": "hidden-customer@example.com", "date": date, "time": "11:00",
                                "car_model": "Trailview", "email_status": "skipped"})

    tape = ToolRecorder()
    initial_count = previous_count = booking_count()
    turns = []
    async with AsyncSqliteSaver.from_conn_string(checkpoint_path) as saver, load_tools() as tools:
        graph = build_graph(saver, tools)
        config = {"configurable": {"thread_id": str(uuid4())}, "callbacks": [tape], "recursion_limit": 24}
        pending = False
        for step in inputs["turns"]:
            if isinstance(step, dict):
                decision = step["decision"]
                if not pending:
                    turns.append({"decision": decision, "answer": "Expected approval is missing.",
                                  "approval_pending": False, "created_bookings": 0})
                    return {"turns": turns, "trajectory": tape.calls, "tool_evidence": tape.evidence,
                            "completed": False, "created_bookings": booking_count() - initial_count}
                graph_input = Command(resume=decision)
                item = {"decision": decision}
            else:
                text = step.replace("{visit_date}", date)
                graph_input = {"messages": [HumanMessage(content=text)], "tool_rounds": 0}
                item = {"user": text}
                if pending:
                    # The API returns the pending card when a chat message arrives during approval.
                    turns.append({**item, "answer": "Review the visit details to continue.",
                                  "approval_pending": True, "created_bookings": 0})
                    continue
            await graph.ainvoke(graph_input, config, durability="sync")
            state = response(await graph.aget_state(config))
            current_count = booking_count()
            pending = state.approval is not None
            turns.append({**item, "answer": state.message or "", "approval_pending": pending,
                          "created_bookings": current_count - previous_count})
            previous_count = current_count
    return {"turns": turns, "trajectory": tape.calls, "tool_evidence": tape.evidence,
            "completed": not pending, "created_bookings": booking_count() - initial_count}


def target(inputs):
    # Keep test bookings, credentials, and Serper usage out of the production stores.
    with TemporaryDirectory() as directory, patch.dict(os.environ, {
        "BOOKINGS_DB_PATH": str(Path(directory) / "bookings.sqlite"),
        "SMTP_HOST": "", "SMTP_FROM_EMAIL": "", "GOOGLE_CALENDAR_CLIENT_ID": "",
        "GOOGLE_CALENDAR_CLIENT_SECRET": "", "GOOGLE_CALENDAR_REFRESH_TOKEN": "",
        "SERPER_API_KEY": "",
    }), tracing_context(enabled=False):
        return asyncio.run(run_scenario(inputs, str(Path(directory) / "checkpoints.sqlite")))


def publish(cases, evaluators):
    client = Client()
    digest = hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest()[:10]
    name = f"autocare-synthetic-{digest}"
    if client.has_dataset(dataset_name=name):
        dataset = client.read_dataset(dataset_name=name)
    else:
        dataset = client.create_dataset(name, description="Synthetic AutoCare regression scenarios")
    local = {case["inputs"]["case"]: case for case in cases}
    examples = list(client.list_examples(dataset_id=dataset.id))
    for example in examples:
        expected = local.get(example.inputs.get("case"))
        if expected is None or expected != {"inputs": example.inputs, "outputs": example.outputs}:
            raise ValueError("The LangSmith dataset no longer matches the synthetic scenarios in this checkout.")
    existing = {example.inputs["case"] for example in examples}
    if missing := [case for key, case in local.items() if key not in existing]:
        client.create_examples(dataset_id=dataset.id, examples=missing)
    client.evaluate(target, data=name, evaluators=evaluators, experiment_prefix="autocare",
                    max_concurrency=0, disable_evaluator_tracing=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="Upload synthetic results to LangSmith.")
    parser.add_argument("--checks-only", action="store_true", help="Skip the additional LLM judge calls.")
    parser.add_argument("--case", choices=[case["inputs"]["case"] for case in CASES], help="Run just one scenario.")
    args = parser.parse_args()
    load_environment()
    if not os.getenv("GROQ_API_KEY", "").strip():
        parser.error("Set GROQ_API_KEY to run the agent against the synthetic scenarios.")
    if args.publish and not os.getenv("LANGSMITH_API_KEY", "").strip():
        parser.error("Set LANGSMITH_API_KEY to publish the experiment.")

    cases = [case for case in CASES if not args.case or case["inputs"]["case"] == args.case]
    evaluators = [code_checks]
    if not args.checks_only:
        model = os.getenv("AUTOCARE_EVAL_JUDGE_MODEL") or os.getenv("GROQ_MODEL") or "openai/gpt-oss-120b"
        evaluators.append(partial(judge_checks, model=model))
    if args.publish:
        publish(cases, evaluators)
        return

    for case in cases:
        inputs, reference = case["inputs"], case["outputs"]
        observed = target(inputs)
        results = code_checks(observed, reference)["results"]
        if not args.checks_only:
            results += judge_checks(inputs, observed, reference, model=model)["results"]
        print(json.dumps({"case": inputs["case"], "scores": {item.key: item.score for item in results}}))


if __name__ == "__main__":
    main()
