"""LLM-facing tools. None takes a tenant or member argument: the gateway holds the session."""
from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.gateway.gateway import ToolGateway


def _gw(config: RunnableConfig) -> ToolGateway:
    return config["configurable"]["gateway"]


@tool
async def get_member_summary(config: RunnableConfig) -> dict:
    """Get the member on this call: product, plan name, network and coverage start/end dates."""
    return await _gw(config).get_member_summary()


@tool
async def get_benefits(config: RunnableConfig) -> dict:
    """Get the member's coverage status (ACTIVE/TERMINATED), coverage dates and plan-year accumulators:
    deductible met, out-of-pocket met and physical therapy visits used (USD, in-network, individual)."""
    return await _gw(config).get_benefits()


@tool
async def list_claims(cursor: int = 0, *, config: RunnableConfig) -> dict:
    """List the member's claims, newest service date first, 5 per page. Pass `cursor` = the previous
    result's `next_cursor` to get the next page. Only fetch more pages if the answer needs them."""
    return await _gw(config).list_claims(cursor)


@tool
async def get_claim(claim_id: str, config: RunnableConfig) -> dict:
    """Get one claim by its claim id (for example C2639555547): status, dates, amounts, member
    responsibility and denial reason. Only claims of the member on this call can be read."""
    return await _gw(config).get_claim(claim_id)


@tool
async def verify_caller(
    config: RunnableConfig, ssn_last4: str | None = None, dob: str | None = None
) -> dict:
    """Check what the caller said against the member's record. Pass the last 4 SSN digits and/or the
    date of birth (YYYY-MM-DD) exactly as the caller gave them. Returns only {match: true|false}; it
    never returns the stored values."""
    return await _gw(config).verify_caller(ssn_last4, dob)


@tool
async def search_plan_documents(query: str, config: RunnableConfig, plan_year: int | None = None) -> dict:
    """Search THIS member's own plan documents (benefit summary, member services guide, claim codes) for
    what the member pays and for plan rules. `query` is a short phrase such as 'specialist visit copay'.
    `plan_year` is the plan year the question is about: leave empty for the current plan year (2026), use
    2027 for 'next year' / 'after renewal'. Only passages of this member's plan are returned."""
    return await _gw(config).search_plan_documents(query, plan_year)


@tool
async def get_plan_status(config: RunnableConfig) -> dict:
    """How much of the member's in-network individual deductible and out-of-pocket maximum is met and
    remaining, and how many physical therapy visits are used and left, for the current plan year.
    Amounts are calculated for you: use them exactly as returned."""
    return await _gw(config).get_plan_status()


@tool
async def check_appeal_window(claim_id: str, config: RunnableConfig) -> dict:
    """For a DENIED claim: the appeal deadline (denial notice date + the plan's filing window), and whether
    the member is still within it today. Dates are calculated for you: use them exactly as returned."""
    return await _gw(config).check_appeal_window(claim_id)


@tool
async def list_grievances(config: RunnableConfig) -> dict:
    """List the grievances already on file for the member (id, category, status, dates)."""
    return await _gw(config).list_grievances()


@tool
async def check_grievance_deadline(grievance_id: str, config: RunnableConfig) -> dict:
    """For one grievance (for example G26-600230): the plan's resolution deadline (received date + the
    plan's timeframe) and whether it is past that deadline today. Calculated for you: use as returned."""
    return await _gw(config).check_grievance_deadline(grievance_id)


TOOLS = [
    get_member_summary, get_benefits, list_claims, get_claim, verify_caller,
    search_plan_documents, get_plan_status, check_appeal_window, list_grievances, check_grievance_deadline,
]
