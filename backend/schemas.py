"""Data contracts for showroom bookings."""
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field, StringConstraints


class Visit(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    email: EmailStr
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="Local date, YYYY-MM-DD")
    time: str = Field(pattern=r"^\d{2}:\d{2}$", description="Local time, HH:MM (24-hour)")
    car_model: str = Field(description="Exact AutoCare model name, or 'General showroom visit'")
