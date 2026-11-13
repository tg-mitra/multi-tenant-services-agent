"""LangGraph graph for the rep assistant.

Step 4: model node <-> tool node loop. The tools go through the ToolGateway, which is passed in
per request via the run config (never as a tool argument the model could set).
"""
from __future__ import annotations

import asyncio
import time
from functools import lru_cache

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.agent.prompts import system_prompt
from app.budget import BudgetExceeded, safe_reply
from app.agent.tools import TOOLS
from app.config import Settings, get_settings
from app.gateway.gateway import ToolGateway


class LLMNotConfigured(RuntimeError):
    """Raised when the OpenAI key is missing."""


def _build_model(settings: Settings) -> ChatOpenAI:
    if not settings.openai_api_key:
        raise LLMNotConfigured("OPENAI_API_KEY is not set")
    kwargs: dict = {
        "model": settings.openai_model,
        "api_key": settings.openai_api_key,
        "timeout": settings.llm_timeout_seconds,
        "max_retries": 1,
    }
    if settings.llm_temperature is not None:
        kwargs["temperature"] = settings.llm_temperature
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    return ChatOpenAI(**kwargs)


def build_graph(settings: Settings | None = None):
    settings = settings or get_settings()
    model = _build_model(settings).bind_tools(TOOLS)
    system = SystemMessage(content=system_prompt(settings.as_of_date))

    async def call_model(state: MessagesState, config: RunnableConfig) -> dict:
        cfg = config.get("configurable") or {}
        extra, budget, trace = cfg.get("extra_system"), cfg.get("budget"), cfg.get("trace")
        if budget:
            budget.check_model_call()  # raises BudgetExceeded: the turn ends with a clean message
        prefix = [system, SystemMessage(content=extra)] if extra else [system]
        messages = [*prefix, *state["messages"]]
        started = time.perf_counter()
        try:
            reply = await model.ainvoke(messages)
        except Exception:
            if trace:
                trace.event("model_call", name=settings.openai_model, status="error",
                            latency_ms=int((time.perf_counter() - started) * 1000))
            raise
        if budget or trace:
            usage = getattr(reply, "usage_metadata", None) or {}
            tin, tout = usage.get("input_tokens"), usage.get("output_tokens")
            estimated = tin is None or tout is None
            if estimated:  # provider gave no token counts: rough estimate (4 characters per token)
                tin = sum(len(str(m.content)) for m in messages) // 4
                tout = len(str(reply.content)) // 4
            cost = budget.add_model_call(tin, tout) if budget else 0.0
            if trace:
                trace.event("model_call", name=settings.openai_model, status="ok",
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            tokens_in=tin, tokens_out=tout, estimated=estimated or None, cost_usd=round(cost, 8))
        return {"messages": [reply]}

    graph = StateGraph(MessagesState)
    graph.add_node("model", call_model)
    graph.add_node("tools", ToolNode(TOOLS))
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", tools_condition)  # -> "tools" or END
    graph.add_edge("tools", "model")
    return graph.compile()


@lru_cache(maxsize=1)
def get_graph():
    return build_graph()


def _text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, list):  # some providers return content blocks
        content = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return content


async def run_turn(
    message: str,
    history: list[BaseMessage] | None = None,
    gateway: ToolGateway | None = None,
    extra_system: str | None = None,
    budget=None,  # app.budget.TurnBudget
    trace=None,  # app.trace.Trace
) -> str:
    """Run one rep message (after any earlier exchanges) through the graph; return the text reply.

    `extra_system` is a per-request system note (for example "a grievance proposal is waiting").
    When a budget is used up (steps, cost or time) the rep gets a fixed, clean message instead."""
    settings = get_settings()
    messages = [*(history or []), HumanMessage(content=message)]
    config = {
        "configurable": {"gateway": gateway, "extra_system": extra_system, "budget": budget, "trace": trace},
        "recursion_limit": settings.max_graph_steps,
    }
    graph_call = get_graph().ainvoke({"messages": messages}, config=config)
    try:
        if budget and settings.budget_turn_seconds > 0:
            result = await asyncio.wait_for(graph_call, timeout=settings.budget_turn_seconds)
        else:
            result = await graph_call
    except BudgetExceeded as exc:
        return _stopped(exc.reason, budget, trace)
    except asyncio.TimeoutError:
        return _stopped("turn_time", budget, trace)
    except GraphRecursionError:
        return _stopped("graph_steps", budget, trace)
    return _text(result["messages"][-1])


def _stopped(reason: str, budget, trace) -> str:
    if budget is not None:
        budget.stopped_by = budget.stopped_by or reason
    if trace:
        trace.decision("agent", "budget_stop", reason=reason)
    return safe_reply(reason)
