"""Shared project paths and environment loading."""
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"


def load_environment():
    load_dotenv(PROJECT_ROOT / ".env")
