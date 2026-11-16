"""General showroom information and local time."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import DATA_DIR


def dealership_info():
    return json.loads((DATA_DIR / "knowledge.json").read_text(encoding="utf-8"))


def showroom_now(info):
    return datetime.now(ZoneInfo(info["timezone"]))
