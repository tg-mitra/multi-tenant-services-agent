"""Per-conversation budgets for steps, cost and latency (Step 9).

Two scopes, both enforced by code (never by the prompt):
  * one rep message ("turn"): model calls, tool calls, wall-clock time;
  * the whole conversation: cumulative cost, steps (model + tool calls) and model-path time.

A limit of 0 disables that limit. Usage is saved in the conversation file so the conversation totals
survive between messages. Rep confirmations/cancellations never go through a budget: a write that has
started must be allowed to finish (see app.turns).
"""
from __future__ import annotations

import time

from app.config import Settings

ZERO_USAGE = {"model_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0,
              "cost_usd": 0.0, "latency_ms": 0}

SAFE_REPLY = {
    "turn_time": "I couldn't finish that within the time allowed. Please try a narrower question.",
    "turn_model_calls": "I couldn't finish that within the allowed number of steps. Please try a narrower question.",
    "turn_tool_calls": "I couldn't finish that within the allowed number of lookups. Please try a narrower question.",
    "graph_steps": "I couldn't finish that within the allowed number of steps. Please try a narrower question.",
}
CONVERSATION_LIMIT_REPLY = ("This conversation has reached its usage limit. "
                            "Please start a new conversation to continue.")
CONVERSATION_REASONS = ("conversation_cost", "conversation_steps", "conversation_time")


class BudgetExceeded(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def safe_reply(reason: str) -> str:
    return CONVERSATION_LIMIT_REPLY if reason in CONVERSATION_REASONS else SAFE_REPLY.get(reason, SAFE_REPLY["turn_model_calls"])


def price(settings: Settings, input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * settings.llm_input_cost_per_million + output_tokens * settings.llm_output_cost_per_million) / 1_000_000


class TurnBudget:
    """Counts what one rep message has used and checks it, together with the conversation so far."""

    def __init__(self, settings: Settings, prior_usage: dict | None = None):
        self.s = settings
        self.prior = {**ZERO_USAGE, **(prior_usage or {})}
        self.model_calls = 0
        self.tool_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        self.stopped_by: str | None = None
        self._started = time.monotonic()

    # ---- measuring -------------------------------------------------------------------
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    def add_model_call(self, input_tokens: int, output_tokens: int) -> float:
        self.model_calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        cost = price(self.s, input_tokens, output_tokens)
        self.cost_usd += cost
        return cost

    def add_tool_call(self) -> None:
        self.tool_calls += 1

    # ---- checking -----------------------------------------------------------------------
    def _conversation_reason(self) -> str | None:
        s, p = self.s, self.prior
        if s.budget_conversation_cost_usd > 0 and p["cost_usd"] + self.cost_usd >= s.budget_conversation_cost_usd:
            return "conversation_cost"
        steps = p["model_calls"] + p["tool_calls"] + self.model_calls + self.tool_calls
        if s.budget_conversation_steps > 0 and steps >= s.budget_conversation_steps:
            return "conversation_steps"
        if s.budget_conversation_seconds > 0 and p["latency_ms"] + self.elapsed_ms() >= s.budget_conversation_seconds * 1000:
            return "conversation_time"
        return None

    def pre_turn_check(self) -> str | None:
        """Conversation-level limits already used up before this message starts."""
        return self._conversation_reason()

    def _turn_time_reason(self) -> str | None:
        if self.s.budget_turn_seconds > 0 and self.elapsed_ms() >= self.s.budget_turn_seconds * 1000:
            return "turn_time"
        return None

    def check_model_call(self) -> None:
        reason = self._turn_time_reason() or self._conversation_reason()
        if reason is None and self.s.budget_turn_model_calls > 0 and self.model_calls >= self.s.budget_turn_model_calls:
            reason = "turn_model_calls"
        if reason:
            self.stopped_by = self.stopped_by or reason
            raise BudgetExceeded(reason)

    def check_tool_call(self) -> str | None:
        reason = self._turn_time_reason() or self._conversation_reason()
        if reason is None and self.s.budget_turn_tool_calls > 0 and self.tool_calls >= self.s.budget_turn_tool_calls:
            reason = "turn_tool_calls"
        if reason:
            self.stopped_by = self.stopped_by or reason
        return reason

    # ---- reporting -------------------------------------------------------------------------
    def usage_delta(self) -> dict:
        return {"model_calls": self.model_calls, "tool_calls": self.tool_calls,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cost_usd": round(self.cost_usd, 8), "latency_ms": self.elapsed_ms()}

    def remaining(self) -> dict:
        """Small snapshot written into trace events."""
        s = self.s
        out = {}
        if s.budget_turn_model_calls:
            out["turn_model_calls"] = max(0, s.budget_turn_model_calls - self.model_calls)
        if s.budget_turn_tool_calls:
            out["turn_tool_calls"] = max(0, s.budget_turn_tool_calls - self.tool_calls)
        if s.budget_conversation_cost_usd:
            out["conversation_cost_usd"] = round(max(0.0, s.budget_conversation_cost_usd - self.prior["cost_usd"] - self.cost_usd), 8)
        return out

    def summary(self) -> dict:
        """What the API returns to the caller."""
        return {
            "turn": {"model_calls": self.model_calls, "tool_calls": self.tool_calls,
                     "cost_usd": round(self.cost_usd, 8), "elapsed_ms": self.elapsed_ms()},
            "conversation": {"cost_usd": round(self.prior["cost_usd"] + self.cost_usd, 8),
                             "cost_limit_usd": self.s.budget_conversation_cost_usd or None,
                             "steps": self.prior["model_calls"] + self.prior["tool_calls"] + self.model_calls + self.tool_calls,
                             "steps_limit": self.s.budget_conversation_steps or None},
            "stopped_by": self.stopped_by,
        }
