"""LangChain knowledge tool and MCP client configuration."""
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import uuid4

from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from database.bookings import booking_db
from database.cars import list_cars
from backend.schemas import Visit
from core.config import PROJECT_ROOT
from core.dealership import dealership_info


@tool
def get_product_info() -> dict:
    """Read current models and pricing from SQLite, and dealership hours and policies."""
    info = dealership_info()
    with booking_db() as db:
        info["cars"] = [{key: value for key, value in car.items() if key != "car_id"}
                        for car in list_cars(db)]
    return info


@tool(args_schema=Visit)
def propose_booking(name: str, email: str, date: str, time: str, car_model: str) -> dict:
    """Request review of a complete visit. Use only after checking availability."""
    return Visit(name=name, email=email, date=date, time=time, car_model=car_model).model_dump()


@tool
def propose_reschedule(booking_id: str, email: str, date: str, time: str) -> dict:
    """Request approval to move an existing booking to a local YYYY-MM-DD date and HH:MM time."""
    return {"booking_id": booking_id, "email": email, "date": date, "time": time}


@tool
def propose_cancellation(booking_id: str, email: str) -> dict:
    """Request approval to cancel an existing booking using its ID and booking email."""
    return {"booking_id": booking_id, "email": email}


@asynccontextmanager
async def load_tools():
    data_env = ("BOOKINGS_DB_PATH",)
    booking_env = (*data_env, "SMTP_HOST", "SMTP_PORT", "SMTP_USER",
                   "SMTP_PASSWORD", "SMTP_FROM_EMAIL")
    search_env = (*data_env, "SERPER_API_KEY", "SERPER_DAILY_LIMIT", "SERPER_THREAD_DAILY_LIMIT")
    calendar_env = (*data_env, "GOOGLE_CALENDAR_ID", "GOOGLE_CALENDAR_CLIENT_ID",
                    "GOOGLE_CALENDAR_CLIENT_SECRET", "GOOGLE_CALENDAR_REFRESH_TOKEN")
    servers = {
        "dealership": (["backend.mcp_server"], booking_env),
        "serper": (["backend.mcp_server", "serper"], search_env),
        "calendar": (["backend.calendar_mcp"], calendar_env),
        "emi": (["backend.emi_mcp"], ()),
    }
    client = MultiServerMCPClient({
        name: {
            "transport": "stdio",
            "command": sys.executable,
            "cwd": str(PROJECT_ROOT),
            "args": ["-m", *module],
            "env": {key: os.environ[key] for key in keys if key in os.environ},
        }
        for name, (module, keys) in servers.items()
    })
    async with AsyncExitStack() as stack:
        remote_tools = {}
        for name in servers:
            session = await stack.enter_async_context(client.session(name))
            items = await load_mcp_tools(session, handle_tool_errors=False)
            remote_tools.update((item.name, item) for item in items)
        yield remote_tools


async def call_mcp(tool, args):
    """Extract FastMCP's structured result from the adapter's ToolMessage."""
    result = await tool.ainvoke({
        "type": "tool_call",
        "name": tool.name,
        "id": str(uuid4()),
        "args": args,
    })
    return result.artifact["structured_content"]
