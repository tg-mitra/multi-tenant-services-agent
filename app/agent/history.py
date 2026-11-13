"""Conversation history window sent to the LLM (Step 3)."""
from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage


def turns_to_messages(turns: list[dict], limit: int) -> list[BaseMessage]:
    """Return the last `limit` exchanges as alternating rep (human) / assistant messages, oldest first.

    One exchange = one rep message plus its reply. Older exchanges are dropped here;
    Step 11 may condense them into a summary instead.
    """
    if limit <= 0:
        return []
    messages: list[BaseMessage] = []
    for turn in turns[-limit:]:
        messages.append(HumanMessage(content=turn["rep"]))
        messages.append(AIMessage(content=turn["assistant"]))
    return messages
