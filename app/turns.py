"""One rep message, start to finish (Steps 2-9).

Order of decisions for a message:
  1. A grievance proposal is waiting for confirmation -> the REP'S OWN WORDS decide, by rule, before any
     model call: confirm (file it), cancel (drop it), or other (ask again; dropped after two unclear turns).
  2. A bare "yes / confirmed" with nothing waiting -> a fixed answer (already filed / nothing to confirm).
  3. Otherwise -> the model, through the gateway, inside the turn and conversation budgets. If the model
     prepared a proposal, the read-back is appended in fixed wording and saved as the pending action.

The model has no tool that files, so no model output and no text inside tool results can cause a write.
Confirm/cancel/fixed answers never touch a budget: a write that has started must be allowed to finish.
Every step is traced (ids, timings, tokens, cost; never message text). The caller must hold the
conversation's turn lock (see app.main).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app.agent.graph import LLMNotConfigured, run_turn
from app.agent.history import turns_to_messages
from app.budget import TurnBudget, safe_reply
from app.calc import deadline_status
from app.config import Settings
from app.gateway.gateway import SessionContext, ToolGateway
from app.gateway.phi import guard_output, redact_text
from app.grievance import (
    CANCELLED_TEXT, EXPIRED_TEXT, FAILED_TEXT, NOTHING_PENDING_TEXT, UNCONFIRMED_TEXT, already_filed_text,
    classify_confirmation, filed_text, is_bare_confirmation, iso, now, pending_expired, readback_text,
)
from app.store.jsonfile import _UNSET, IdempotencyStore, JsonConversationStore
from app.trace import Trace

log = logging.getLogger("uvicorn.error")


class TurnError(Exception):
    def __init__(self, status: int, detail: str, conversation_id: str | None):
        super().__init__(detail)
        self.status, self.detail, self.conversation_id = status, detail, conversation_id


@dataclass
class TurnResult:
    conversation_id: str
    reply: str
    budget: dict = field(default_factory=dict)


def _event(kind: str, **extra) -> dict:
    return {"type": kind, "at": iso(now()), **extra}


def _waiting_note(pending: dict) -> str:
    return (
        "A grievance proposal is waiting for the rep's explicit confirmation "
        f"(category: {pending['category']}; description: {pending['description']!r}). The rep's latest message was "
        "not a clear confirm or cancel. If it is unclear, ask them to reply \"confirm\" or \"cancel\". If they want "
        "changes, call propose_grievance again with the corrected details. If they ask something else, answer it. "
        "Never say or imply anything has been filed."
    )


async def _confirm(settings: Settings, idem: IdempotencyStore, ctx: SessionContext, pending: dict, trace: Trace):
    """Run the write for a confirmed proposal. Returns (reply, new_pending, new_event)."""
    async with ToolGateway(ctx, settings, idem_store=idem, trace=trace) as gw:  # no budget: a write must finish
        await gw.ensure_benefits()  # product, for the plan's resolution timeframe (best effort)
        outcome = await gw.file_grievance(pending)
        timeframe = gw.timeframe_days()
    state = outcome["state"]
    trace.event("confirmation", name="filing", state=state, attempts=outcome.get("attempts"),
                reason=outcome.get("reason"), http_status=outcome.get("http_status"))
    if state in ("filed", "already_filed"):
        g = outcome["grievance"]
        if state == "already_filed":
            return already_filed_text(g["grievance_id"]), None, _event("filed", grievance_id=g["grievance_id"])
        deadline = days = None
        if timeframe and g.get("received_date"):
            days = timeframe[0]
            try:
                deadline = deadline_status(g["received_date"], days, settings.as_of_date)["deadline"]
            except ValueError:
                deadline = None
        text = filed_text(g, pending["category"], deadline, days, retried=outcome.get("attempts", 1) > 1)
        return text, None, _event("filed", grievance_id=g["grievance_id"])
    if state == "unconfirmed":  # keep the proposal: "confirm" again retries with the SAME key and bytes
        kept = {**pending, "attempts": pending.get("attempts", 0) + outcome.get("attempts", 0)}
        return UNCONFIRMED_TEXT, kept, _event("unconfirmed")
    return FAILED_TEXT, None, _event("failed", reason=outcome.get("reason") or outcome.get("http_status"))


async def process_turn(
    req, settings: Settings, store: JsonConversationStore, idem: IdempotencyStore,
    conversation_id: str, is_new: bool,
) -> TurnResult:
    known_id = None if is_new else conversation_id
    record = None if is_new else store.load(req.tenant, req.member_id, conversation_id)
    if not is_new and record is None:
        raise TurnError(404, "Conversation not found", None)
    history = turns_to_messages(record["turns"], settings.history_turns) if record else []
    pending = record.get("pending_action") if record else None
    last_event = record.get("last_grievance_event") if record else None
    ctx = SessionContext(tenant=req.tenant, member_id=req.member_id, conversation_id=conversation_id)

    budget = TurnBudget(settings, record.get("usage") if record else None)
    trace = Trace(settings, req.tenant, req.member_id, conversation_id, (len(record["turns"]) if record else 0) + 1)
    trace.budget = budget
    trace.event("turn", name="start", status="ok")

    started = time.perf_counter()
    new_pending, new_event = _UNSET, _UNSET
    reply: str | None = None
    extra_system: str | None = None
    used_model = False
    path = "model"

    def fail(error: TurnError, cause: Exception | None = None):
        trace.event("turn", name="end", status="error", path=path, reason=error.detail,
                    http_status=error.status, latency_ms=int((time.perf_counter() - started) * 1000))
        return error

    # ---- 1. a proposal is waiting: the rep's own words decide --------------------------------------
    if pending and pending_expired(pending):
        trace.decision("grievance_proposal", "expired")
        pending, new_pending, new_event = None, None, _event("expired")
        last_event = new_event
    if pending:
        intent = classify_confirmation(req.message)
        trace.event("confirmation", name="classify", intent=intent)
        if intent == "confirm":
            path = "confirm"
            try:
                reply, new_pending, new_event = await _confirm(settings, idem, ctx, pending, trace)
            except Exception as exc:  # nothing new was saved: the proposal stays, "confirm" is safe to repeat
                log.error("grievance confirm failed conv=%s type=%s", conversation_id, type(exc).__name__)
                raise fail(TurnError(500, "Could not complete the filing. Reply \"confirm\" to try again; "
                                          "it cannot be filed twice.", known_id)) from exc
        elif intent == "cancel":
            path = "cancel"
            reply, new_pending, new_event = CANCELLED_TEXT, None, _event("cancelled")
            trace.decision("grievance_proposal", "cancelled")
        else:
            unclear = pending.get("other_turns", 0) + 1
            if unclear >= 2:  # unclear twice in a row: withdraw it rather than leave it hanging
                new_pending, new_event = None, _event("expired")
                last_event = new_event
                trace.decision("grievance_proposal", "withdrawn", reason="unclear_twice")
            else:
                new_pending = {**pending, "other_turns": unclear}
                extra_system = _waiting_note(pending)

    # ---- 2. a bare "yes" with nothing waiting: fixed answer, no model, no write --------------------------
    elif is_bare_confirmation(req.message) and last_event:
        path = "fixed_reply"
        if last_event.get("type") == "filed" and last_event.get("grievance_id"):
            reply = already_filed_text(last_event["grievance_id"])
        else:
            reply = EXPIRED_TEXT if last_event.get("type") in ("expired", "cancelled") else NOTHING_PENDING_TEXT
        trace.decision("grievance_proposal", "confirmation_ignored", reason="nothing_pending")

    # ---- 3. the model, inside the budgets ------------------------------------------------------------------
    if reply is None:
        stop = budget.pre_turn_check()  # conversation limits already used up before this message
        if stop:
            path = "budget_stop"
            budget.stopped_by = stop
            trace.decision("conversation", "budget_stop", reason=stop)
            reply = safe_reply(stop)
    if reply is None:
        used_model = True
        model_message = req.message
        if settings.phi_filter_enabled:
            # A full SSN, phone or email typed by the rep is not sent to the model; the caller's last-4
            # and DOB stay because verify_caller needs them.
            model_message, _ = redact_text(req.message, skip={"dob", "ssn_last4"})
        try:
            async with ToolGateway(ctx, settings, idem_store=idem, budget=budget, trace=trace) as gw:
                reply = await run_turn(model_message, history, gateway=gw, extra_system=extra_system,
                                       budget=budget, trace=trace)
                if gw.proposal and not budget.stopped_by:  # the model prepared a grievance: fixed read-back
                    reply = f"{reply.strip()}\n\n{readback_text(gw.proposal)}"
                    new_pending, new_event = gw.proposal, _event("proposed")
                    trace.decision("grievance_proposal", "proposed", category=gw.proposal["category"])
        except LLMNotConfigured:
            raise fail(TurnError(503, "LLM is not configured", known_id))
        except Exception as exc:
            body = getattr(exc, "body", None)
            code = body.get("code") if isinstance(body, dict) else None
            log.error("LLM call failed: type=%s status=%s code=%s", type(exc).__name__,
                      getattr(exc, "status_code", None), code)
            raise fail(TurnError(502, "LLM request failed", known_id))
    latency_ms = int((time.perf_counter() - started) * 1000)

    # ---- output guard and redaction (Step 5) --------------------------------------------------------------
    saved_rep = req.message
    if settings.phi_filter_enabled:
        reply, blocked = guard_output(reply)
        if blocked:
            log.warning("phi output guard blocked reply conv=%s kinds=%s", conversation_id, ",".join(blocked))
            trace.event("guard", name="output_guard", decision="blocked", kinds=blocked)
        saved_rep, counts = redact_text(req.message)
        reply_to_save, reply_counts = redact_text(reply)
        if counts or reply_counts:
            log.info("phi redaction conv=%s in=%s out=%s", conversation_id, counts, reply_counts)
            trace.event("guard", name="redaction", redactions={"rep_message": counts, "reply": reply_counts})
    else:
        reply_to_save = reply

    usage = budget.usage_delta() if used_model else None
    try:
        await store.append_turn(
            req.tenant, req.member_id, conversation_id,
            rep_message=saved_rep, assistant_reply=reply_to_save,
            model=settings.openai_model if used_model else "system",
            latency_ms=latency_ms, is_new=is_new, pending=new_pending, last_event=new_event, usage=usage,
        )
    except Exception as exc:
        log.error("Saving conversation failed: type=%s", type(exc).__name__)
        raise fail(TurnError(500, "Could not save the conversation", known_id)) from exc

    trace.event("turn", name="end", status="stopped" if budget.stopped_by else "ok", path=path,
                latency_ms=latency_ms, model_calls=budget.model_calls, tool_calls=budget.tool_calls,
                cost_usd=round(budget.cost_usd, 8), stopped_by=budget.stopped_by)
    return TurnResult(conversation_id, reply, budget.summary())
