"""Single-agent graph with a durable customer approval step."""
import json
import logging
import os
import re
from pathlib import Path
from typing import Literal
from uuid import uuid4

from groq import APIConnectionError, APIError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_groq import ChatGroq
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError
from typing_extensions import NotRequired, TypedDict

from core.dealership import dealership_info, showroom_now
from core.search_policy import CAR_TOPIC, MODEL_PATTERN, OUTSIDE_TOPIC, external_query
from database.bookings import booking_db
from database.cars import has_model

from .policy import (
    ACCEPT, BOOKING_INTENT, BOOKING_REFERENCE, CONTACT_REQUEST, DECLINE, DISCOUNT_REPLY,
    DISCOUNT_REQUEST, FINANCING_REQUEST, GREETING,
    MANAGE_BOOKING, NAME_REPLY, SCOPE_REPLY, VISIT_DETAILS, VISIT_FINISHED, VISIT_OFFER,
)
from .tools import call_mcp, get_product_info, propose_booking, propose_cancellation, propose_reschedule

MAX_TOOL_ROUNDS = 6
logger = logging.getLogger(__name__)
PROPOSALS = {
    "propose_booking": "create",
    "propose_reschedule": "reschedule",
    "propose_cancellation": "cancel",
}
PROMPT_PATH = Path(__file__).with_name("prompts") / "customer.md"
LIVE_TOOLS = {"get_product_info", "get_booking", "check_slots", "web_search"}


def provider_error_reply(error):
    status = getattr(error, "status_code", None)
    body = error.body if isinstance(error.body, dict) else {}
    details = body.get("error", body)
    code = details.get("code") if isinstance(details, dict) else None
    if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", code):
        code = None
    logger.warning("Groq chat failed (%s, HTTP status %s, error code %s).", type(error).__name__, status, code)
    if status == 429:
        return "AutoCare chat has reached its usage limit. Please try again later."
    if status in (401, 403, 404):
        return "AutoCare chat is temporarily unavailable due to a provider configuration issue."
    if status in (400, 413, 422):
        return "I couldn't process that request. Try rephrasing it, or use /new to start a fresh chat."
    if isinstance(error, APIConnectionError):
        return "AutoCare chat couldn't connect to its provider. Please try again in a moment."
    return "The chat provider is unavailable. Please try again later."


def model_history(messages):
    """Keep old tool-call IDs without presenting old database results as current."""
    last_user = max(index for index, message in enumerate(messages) if isinstance(message, HumanMessage))
    return [
        message.model_copy(update={"content": "This earlier result expired. Use the tool for current details."})
        if index < last_user and isinstance(message, ToolMessage) and message.name in LIVE_TOOLS
        else message
        for index, message in enumerate(messages)
    ]


def turn_context(messages):
    humans = [message for message in messages if isinstance(message, HumanMessage)]
    current = humans[-1]
    previous = humans[-3:-1]
    query = external_query(
        current.text, automotive_context=any(CAR_TOPIC.search(msg.text) or MODEL_PATTERN.search(msg.text)
                                            for msg in previous),
    )
    return current, query, visit_exchange(messages)


def visit_exchange(messages):
    """Find the active booking exchange, including short replies to the bot's questions."""
    start = None
    previous_reply = ""
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage) and not message.tool_calls:
            previous_reply = message.text
            if VISIT_FINISHED.match(previous_reply):
                start = None
        elif isinstance(message, HumanMessage):
            text = message.text.strip()
            if DECLINE.fullmatch(text) or OUTSIDE_TOPIC.search(text):
                start = None
            elif BOOKING_INTENT.search(text) or MANAGE_BOOKING.search(text) or (
                ACCEPT.fullmatch(text) and VISIT_OFFER.search(previous_reply)
            ):
                if start is None:
                    start = index
            elif start is not None and not (
                VISIT_DETAILS.search(text) or BOOKING_REFERENCE.search(text) or "@" in text
                or ACCEPT.fullmatch(text)
                or (CONTACT_REQUEST.search(previous_reply) and NAME_REPLY.fullmatch(text))
            ):
                start = None
            previous_reply = ""
    return messages[start:] if start is not None else []


def last_available_slots(exchange):
    for message in reversed(exchange):
        if isinstance(message, ToolMessage) and message.name == "check_slots":
            result = json.loads(message.text)
            return result.get("slots", []) if result.get("ok") else []
    return []


def can_request_contact(exchange):
    human_replies = [msg.text for msg in exchange if isinstance(msg, HumanMessage)]
    if any(MANAGE_BOOKING.search(text) for text in human_replies):
        return True
    for slot in last_available_slots(exchange):
        hour, minute = map(int, slot.split(":"))
        suffix = "p" if hour >= 12 else "a"
        twelve_hour = str(hour % 12 or 12)
        minutes = f":{minute:02d}" if minute else "(?::00)?"
        if any(re.search(rf"\b(?:{slot}|{twelve_hour}{minutes}\s*{suffix}\.?m\.?)\b", text, re.I)
               for text in human_replies):
            return True
    return False


class Approval(TypedDict):
    booking_id: str
    name: str
    email: str
    date: str
    time: str
    car_model: str
    timezone: str
    duration_minutes: int
    operation: NotRequired[Literal["reschedule", "cancel"]]
    approval_id: NotRequired[str]
    previous_date: NotRequired[str]
    previous_time: NotRequired[str]
    revision: NotRequired[int]


class State(MessagesState):
    tool_rounds: int
    proposal: Approval | None
    decision: Literal["confirm", "reject", "modify"] | None
    booking: dict | None
    search_scope_id: NotRequired[str]


def build_graph(checkpointer, remote_tools, model=None):
    """Compile the agent, keeping the write tools outside the model's tool list."""
    if model is None and os.getenv("GROQ_API_KEY"):
        model = ChatGroq(
            model=os.getenv("GROQ_MODEL") or "openai/gpt-oss-120b",
            temperature=0.2,
            reasoning_format="hidden",
            timeout=20,
            max_retries=1,
        )
    allowed = {
        tool.name: tool for tool in
        (get_product_info, remote_tools["check_slots"], remote_tools["get_booking"],
         propose_booking, propose_reschedule, propose_cancellation, remote_tools["calculate_emi"])
    }
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    booking_chat = browse_chat = None
    if model is not None:
        booking_chat = model.bind_tools(list(allowed.values()))
        browse_chat = model.bind_tools([tool for name, tool in allowed.items() if name not in PROPOSALS])

    async def assistant(state: State):
        current, query, exchange = turn_context(state["messages"])
        chat = booking_chat if exchange else browse_chat
        if OUTSIDE_TOPIC.search(current.text) and not BOOKING_INTENT.search(current.text):
            return {"messages": [AIMessage(content=SCOPE_REPLY)]}
        if GREETING.fullmatch(current.text):
            return {"messages": [AIMessage(content="Hi! How can I help you with AutoCare Motors today?")]}
        if DISCOUNT_REQUEST.search(current.text) and not (exchange or FINANCING_REQUEST.search(current.text)):
            return {"messages": [AIMessage(content=DISCOUNT_REPLY)]}
        if chat is None:
            return {"messages": [AIMessage(content="Chat is unavailable. Set GROQ_API_KEY and restart the API.")]}
        contact_allowed = can_request_contact(exchange)
        info = dealership_info()
        now = showroom_now(info)
        prompt = SystemMessage(content=(
            f"{system_prompt}\nShowroom time: {now.isoformat()} ({info['timezone']}).\n"
            f"External automotive search results supplied for this turn: {'yes' if query else 'no'}.\n"
            f"The customer is arranging or managing a visit: {'yes' if exchange else 'no'}.\n"
            f"Requesting contact details permitted on this turn: {'yes' if contact_allowed else 'no'}."
        ))
        try:
            answer = await chat.ainvoke([prompt, *model_history(state["messages"])])
        except APIError as error:
            answer = AIMessage(content=provider_error_reply(error))
        if answer.invalid_tool_calls or (not answer.tool_calls and not answer.text.strip()):
            answer = AIMessage(content="I couldn't finish that response. Please try rephrasing your request.")
        if not answer.tool_calls and not contact_allowed and CONTACT_REQUEST.search(answer.text):
            slots = last_available_slots(exchange)
            answer = AIMessage(content=(
                f"Available times are {', '.join(slots)}. Which works for you?" if slots
                else "What day and time would you like to visit?" if exchange
                else "What would you like to know about AutoCare Motors?"
            ))
        if not answer.tool_calls and DISCOUNT_REQUEST.search(current.text) and DISCOUNT_REPLY not in answer.text:
            answer = AIMessage(content=f"{answer.text}\n\n{DISCOUNT_REPLY}")
        return {"messages": [answer]}

    def after_assistant(state: State):
        return "tools" if state["messages"][-1].tool_calls else END

    def route_turn(state: State):
        _, query, _ = turn_context(state["messages"])
        return "search" if query else "assistant"

    async def search(state: State, config: RunnableConfig):
        current, query, _ = turn_context(state["messages"])
        try:
            result = await call_mcp(remote_tools["web_search"], {
                "query": query,
                "thread_id": state.get("search_scope_id") or config["configurable"]["thread_id"],
                "turn_id": current.id,
            })
        except Exception:
            result = {"ok": False, "message": "Web search is temporarily unavailable."}
        if not result["ok"] or not result.get("results"):
            return {"messages": [AIMessage(content=result.get("message") or "I couldn't find relevant web results.")]}
        call_id = str(uuid4())
        return {"messages": [
            AIMessage(content="", tool_calls=[{"name": "web_search", "args": {"query": query}, "id": call_id}]),
            ToolMessage(name="web_search", tool_call_id=call_id, content=json.dumps(result)),
        ]}

    def after_search(state: State):
        return "assistant" if isinstance(state["messages"][-1], ToolMessage) else END

    async def prepare_proposal(name, args):
        request = await allowed[name].ainvoke(args)
        operation = PROPOSALS[name]
        info = dealership_info()
        if operation == "create":
            with booking_db() as db:
                if request["car_model"] != "General showroom visit" and not has_model(db, request["car_model"]):
                    return {"ok": False, "message": "Choose a current AutoCare model or a general visit."}, None
            visit = {**request, "booking_id": str(uuid4())}
        else:
            lookup = await call_mcp(remote_tools["get_booking"], {
                "booking_id": request["booking_id"], "email": request["email"],
            })
            if not lookup["ok"]:
                return lookup, None
            if not lookup["can_change"]:
                return {"ok": False, "message": "This booking is cancelled or has already started."}, None
            current = lookup["booking"]
            visit = {
                **{key: current[key] for key in ("booking_id", "name", "email", "car_model", "date", "time", "revision")},
                "operation": operation,
                "approval_id": str(uuid4()),
            }
            if operation == "reschedule":
                if (current["date"], current["time"]) == (request["date"], request["time"]):
                    return {"ok": False, "message": "Your booking is already at that date and time."}, None
                visit.update(previous_date=current["date"], previous_time=current["time"],
                             date=request["date"], time=request["time"])

        if operation != "cancel":
            slots = await call_mcp(remote_tools["check_slots"], {"date": visit["date"]})
            if not slots["ok"]:
                return slots, None
            if visit["time"] not in slots["slots"]:
                return {**slots, "ok": False, "message": "The requested time is unavailable."}, None
        proposal = {
            **visit, "timezone": info["timezone"], "duration_minutes": info["visits"]["duration_minutes"],
        }
        return {"ok": True, "message": "Waiting for customer approval.", "visit": visit}, proposal

    async def run_tools(state: State):
        _, _, exchange = turn_context(state["messages"])
        rounds = state.get("tool_rounds", 0) + 1
        calls = state["messages"][-1].tool_calls
        if not exchange and any(call["name"] in PROPOSALS for call in calls):
            skipped = [
                ToolMessage(content="No tools were run because the customer did not request a booking change.",
                            name=call["name"], tool_call_id=call["id"], status="error")
                for call in calls
            ]
            reply = AIMessage(content=(
                "I can check showroom availability if you share a day and time. "
                "Tell me if you’d like to make or change a booking."
            ))
            return {"messages": [*skipped, reply], "tool_rounds": rounds, "proposal": None}
        results = []
        proposal = None
        for call in calls:
            name, args = call["name"], call["args"]
            try:
                if rounds > MAX_TOOL_ROUNDS:
                    result = {"ok": False, "message": "Tool limit reached."}
                elif name in PROPOSALS:
                    if len(calls) == 1:
                        result, proposal = await prepare_proposal(name, args)
                    else:
                        result = {"ok": False, "message": f"Call {name} on its own."}
                elif name == "web_search":
                    result = {"ok": False, "message": "This turn did not request an external automotive search through this tool."}
                elif name in ("check_slots", "get_booking", "calculate_emi"):
                    result = await call_mcp(allowed[name], args)
                elif name == "get_product_info":
                    result = await get_product_info.ainvoke(args)
                else:
                    result = {"ok": False, "message": "That tool is unavailable."}
            except ValidationError:
                result = {"ok": False, "message": "Check the name, email, date, and time."}
            except Exception:
                result = {"ok": False, "message": "That tool is temporarily unavailable."}
            results.append(ToolMessage(content=json.dumps(result), name=name, tool_call_id=call["id"]))
        return {"messages": results, "tool_rounds": rounds, "proposal": proposal}

    def after_tools(state: State):
        if state.get("proposal"):
            return "approval"
        if isinstance(state["messages"][-1], AIMessage):
            return END
        return "tool_limit" if state["tool_rounds"] > MAX_TOOL_ROUNDS else "assistant"

    def tool_limit(state: State):
        return {"messages": [AIMessage(content="I reached the tool limit. Please narrow your request and try again.")]}

    def approval(state: State):
        # LangGraph restarts this node on resume. All side effects are in later nodes.
        return {"decision": interrupt(state["proposal"])}

    def after_approval(state: State):
        if state["decision"] != "confirm":
            return "declined"
        return "change" if state["proposal"].get("operation") else "book"

    def declined(state: State):
        if state["decision"] == "modify":
            text = ("What date or time should I use instead?" if state["proposal"].get("operation") == "reschedule"
                    else "What would you like to change about the visit?")
        else:
            text = "I discarded the change request." if state["proposal"].get("operation") else "I discarded the visit request."
        return {"messages": [AIMessage(content=text)], "proposal": None, "decision": None}

    async def change(state: State):
        visit = state["proposal"]
        reschedule = visit["operation"] == "reschedule"
        args = {
            "booking_id": visit["booking_id"], "email": visit["email"],
            "expected_revision": visit["revision"], "change_id": visit["approval_id"],
        }
        if reschedule:
            args.update(date=visit["date"], time=visit["time"])
        tool_name = "reschedule_booking" if reschedule else "cancel_booking"
        result = await call_mcp(remote_tools[tool_name], args)
        if result["ok"]:
            booking = result["booking"]
            when = f"{booking['date']} at {booking['time']} ({visit['timezone']})"
            status = f"rescheduled to {when}" if reschedule else f"cancelled for {when}"
            text = f"Your showroom visit was {status}. Booking ID: {booking['booking_id']}."
        else:
            text = result["message"]
        return {"messages": [AIMessage(content=text)], "proposal": None, "decision": None}

    async def book(state: State):
        proposal = state["proposal"]
        args = {key: proposal[key] for key in ("booking_id", "name", "email", "date", "time", "car_model")}
        # The MCP server reserves and queues delivery atomically, with a stable ID on replay.
        result = await call_mcp(remote_tools["create_booking"], args)
        if not result["ok"]:
            return {
                "messages": [AIMessage(content=result["message"])],
                "proposal": None,
                "decision": None,
                "booking": None,
            }
        return booking_confirmed(result["booking"], proposal)

    def email(state: State):
        # Older checkpoints can still be waiting at this node.
        return booking_confirmed(state["booking"], state["proposal"])

    def booking_confirmed(booking, proposal):
        text = (
            f"Your showroom visit for {booking['car_model']} is booked for "
            f"{booking['date']} at {booking['time']} ({proposal['timezone']}). "
            f"Booking ID: {booking['booking_id']}. Your confirmation email and calendar invitation are queued."
        )
        return {
            "messages": [AIMessage(content=text)],
            "proposal": None,
            "decision": None,
            "booking": None,
        }

    graph = StateGraph(State)
    graph.add_node("assistant", assistant)
    graph.add_node("search", search)
    graph.add_node("tools", run_tools)
    graph.add_node("tool_limit", tool_limit)
    graph.add_node("approval", approval)
    graph.add_node("declined", declined)
    graph.add_node("change", change)
    graph.add_node("book", book)
    graph.add_node("email", email)
    graph.add_conditional_edges(START, route_turn, ["search", "assistant"])
    graph.add_conditional_edges("search", after_search, ["assistant", END])
    graph.add_conditional_edges("assistant", after_assistant, ["tools", END])
    graph.add_conditional_edges("tools", after_tools, ["approval", "tool_limit", "assistant", END])
    graph.add_conditional_edges("approval", after_approval, ["book", "change", "declined"])
    graph.add_edge("book", END)
    graph.add_edge("tool_limit", END)
    graph.add_edge("declined", END)
    graph.add_edge("change", END)
    graph.add_edge("email", END)
    return graph.compile(checkpointer=checkpointer)
