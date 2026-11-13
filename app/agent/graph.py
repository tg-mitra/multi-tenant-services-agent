"""LangGraph graph for the rep assistant.

Step 4: model node <-> tool node loop. The tools go through the ToolGateway, which is passed in
per request via the run config (never as a tool argument the model could set).
"""
from __future__ import annotations

from functools import lru_cache

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.agent.prompts import system_prompt
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

    async def call_model(state: MessagesState) -> dict:
        reply = await model.ainvoke([system, *state["messages"]])
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
) -> str:
    """Run one rep message (after any earlier exchanges) through the graph; return the text reply."""
    settings = get_settings()
    messages = [*(history or []), HumanMessage(content=message)]
    config = {
        "configurable": {"gateway": gateway},
        "recursion_limit": settings.max_graph_steps,
    }
    try:
        result = await get_graph().ainvoke({"messages": messages}, config=config)
    except GraphRecursionError:
        return (
            "I could not finish looking that up within the allowed number of steps. "
            "Please try a narrower question."
        )
    return _text(result["messages"][-1])
