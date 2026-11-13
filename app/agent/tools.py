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


TOOLS = [get_member_summary, get_benefits, list_claims, get_claim]
