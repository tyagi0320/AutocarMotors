"""Calculate showroom financing estimates with a fixed annual interest rate."""
from decimal import Decimal, ROUND_HALF_UP
from math import isfinite
from typing import Any

from mcp.server.fastmcp import FastMCP

server = FastMCP("EMI Calculator")
ANNUAL_RATE = Decimal("8.5")


@server.tool(structured_output=True)
def calculate_emi(loan_amount_inr: float, tenure_months: int) -> dict[str, Any]:
    """Estimate EMI in INR from the financed amount after down payment and tenure in whole months."""
    if not isfinite(loan_amount_inr) or not 0.01 <= loan_amount_inr <= 1_000_000_000_000 or not 1 <= tenure_months <= 600:
        return {"ok": False, "message": "Provide a loan amount from ₹0.01 to ₹1 trillion and a term of 1 to 600 months."}
    principal = Decimal(str(loan_amount_inr))
    monthly_rate = ANNUAL_RATE / 1200
    growth = (1 + monthly_rate) ** tenure_months
    payment = principal * monthly_rate * growth / (growth - 1)
    total = payment * tenure_months
    return {
        "ok": True,
        "annual_interest_percent": str(ANNUAL_RATE),
        "tenure_months": tenure_months,
        "monthly_emi_inr": format(payment.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ",.2f"),
        "total_payment_inr": format(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ",.2f"),
        "total_interest_inr": format((total - principal).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ",.2f"),
    }


if __name__ == "__main__":
    server.run(transport="stdio")
