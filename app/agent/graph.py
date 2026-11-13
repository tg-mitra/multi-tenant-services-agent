"""LangGraph graph for the rep assistant.

Step 1: a single model node. No tools, no filter, no storage. Later steps add
nodes (retrieval, gateway tools, guards) to this same graph.
"""
from __future__ import annotations

from functools import lru_cache

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, MessagesState, StateGraph

from app.config import Settings, get_settings


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
    model = _build_model(settings)

    async def call_model(state: MessagesState) -> dict:
        reply = await model.ainvoke(state["messages"])
        return {"messages": [reply]}

    graph = StateGraph(MessagesState)
    graph.add_node("model", call_model)
    graph.add_edge(START, "model")
    graph.add_edge("model", END)
    return graph.compile()


@lru_cache(maxsize=1)
def get_graph():
    return build_graph()


async def run_turn(message: str) -> str:
    """Send one rep message through the graph and return the model's text reply."""
    result = await get_graph().ainvoke({"messages": [HumanMessage(content=message)]})
    last: BaseMessage = result["messages"][-1]
    content = last.content
    if isinstance(content, list):  # some providers return content blocks
        content = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return content
