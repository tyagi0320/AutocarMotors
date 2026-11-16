"""FastAPI entry point for chat and approval."""
import asyncio
import os
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID
from weakref import WeakValueDictionary

from fastapi import FastAPI, HTTPException, Request
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from langsmith import tracing_context
from pydantic import BaseModel, Field, UUID4, field_validator

from ai.agent import Approval, build_graph
from ai.tools import load_tools
from backend.booking_delivery import deliver_calendar_jobs, deliver_email_jobs
from core.config import DATA_DIR, load_environment
from core.observability import create_tracing_client, trace_destination


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_environment()
    db_path = os.getenv("CHECKPOINT_PATH") or str(DATA_DIR / "checkpoints.sqlite")
    async with AsyncSqliteSaver.from_conn_string(db_path) as saver, load_tools() as tools:
        app.state.graph = build_graph(saver, tools)
        app.state.tracing_client = create_tracing_client()
        app.state.locks = WeakValueDictionary()
        # Tasks inherit the trace context. Keep delivery outside the request tracer.
        with tracing_context(enabled=False):
            workers = [
                asyncio.create_task(deliver_email_jobs(tools["send_email"])),
                asyncio.create_task(deliver_calendar_jobs(tools["sync_calendar_booking"])),
            ]
        try:
            yield
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            if app.state.tracing_client is not None:
                await asyncio.to_thread(app.state.tracing_client.flush, timeout=2)


app = FastAPI(title="AutoCare Motors", lifespan=lifespan)


class ChatRequest(BaseModel):
    thread_id: UUID4
    message: str | None = Field(default=None, min_length=1, max_length=6000)
    message_id: str | None = Field(default=None, min_length=1, max_length=100)
    search_scope_id: UUID | None = None

    @field_validator("message")
    @classmethod
    def validate_message(cls, message):
        if message is not None:
            message = message.strip()
            if not message:
                raise ValueError("Enter a message.")
        return message


class ResumeRequest(BaseModel):
    thread_id: UUID4
    approval_id: UUID4
    decision: Literal["confirm", "reject", "modify"]


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatResponse(BaseModel):
    status: Literal["response", "waiting_for_approval"]
    message: str | None
    messages: list[ChatMessage]
    approval: Approval | None


def response(snapshot):
    pending = snapshot.interrupts[0].value if snapshot.interrupts else None
    messages = []
    for message in snapshot.values.get("messages", []):
        if isinstance(message, HumanMessage):
            messages.append(ChatMessage(role="user", content=message.text))
        elif isinstance(message, AIMessage) and not message.tool_calls and message.text.strip():
            messages.append(ChatMessage(role="assistant", content=message.text))
    last = messages[-1] if messages else None
    return ChatResponse(
        status="waiting_for_approval" if pending else "response",
        message="Review the visit details to continue." if pending
        else last.content if last and last.role == "assistant" else None,
        messages=messages,
        approval=pending,
    )


async def run_graph(graph, graph_input, config, tracing_client):
    operation = "recovery" if graph_input is None else "approval" if isinstance(graph_input, Command) else "chat"
    try:
        with tracing_context(
            enabled=tracing_client is not None,
            client=tracing_client,
            tags=["autocare", operation],
            **(trace_destination() if tracing_client is not None else {}),
        ):
            await graph.ainvoke(
                graph_input, {**config, "run_name": f"AutoCare {operation}"}, durability="sync",
            )
    except Exception:
        # Provider exceptions can contain request details. Send a fixed message.
        raise HTTPException(503, "The request could not finish. Reload this chat to try again.") from None
    return await graph.aget_state(config)


async def load_thread(graph, config, tracing_client):
    snapshot = await graph.aget_state(config)
    if snapshot.next and not snapshot.interrupts:
        # Continue a saved run that stopped between nodes before processing new input.
        return await run_graph(graph, None, config, tracing_client)
    return snapshot


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request):
    graph = request.app.state.graph
    tracing_client = request.app.state.tracing_client
    thread_id = str(body.thread_id)
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 24}
    async with request.app.state.locks.setdefault(thread_id, asyncio.Lock()):
        snapshot = await load_thread(graph, config, tracing_client)
        if snapshot.interrupts or body.message is None:
            return response(snapshot)
        if body.message_id and any(
            isinstance(message, HumanMessage) and message.id == body.message_id
            for message in snapshot.values.get("messages", [])
        ):
            return response(snapshot)
        graph_input = {
            "messages": [HumanMessage(content=body.message, id=body.message_id)],
            "tool_rounds": 0,
            "search_scope_id": str(body.search_scope_id or body.thread_id),
        }
        snapshot = await run_graph(graph, graph_input, config, tracing_client)
        return response(snapshot)


@app.post("/resume", response_model=ChatResponse)
async def resume(body: ResumeRequest, request: Request):
    graph = request.app.state.graph
    tracing_client = request.app.state.tracing_client
    thread_id = str(body.thread_id)
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 24}
    async with request.app.state.locks.setdefault(thread_id, asyncio.Lock()):
        snapshot = await load_thread(graph, config, tracing_client)
        proposal = snapshot.interrupts[0].value if snapshot.interrupts else None
        if not proposal or proposal.get("approval_id", proposal["booking_id"]) != str(body.approval_id):
            raise HTTPException(409, "That approval is no longer pending. Reload this chat for its current status.")
        snapshot = await run_graph(graph, Command(resume=body.decision), config, tracing_client)
        return response(snapshot)
