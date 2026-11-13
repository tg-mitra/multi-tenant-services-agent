"""Tool gateway (Step 4): the single place where the agent reaches member data.

What it enforces now:
  * tenant and member come from the session, never from the model (tools have no such arguments);
  * the tenant's own bearer token is used, so the API itself is scoped to the tenant;
  * a claim is returned only if it belongs to the session member (the API checks tenant only);
  * only validated values go into URLs (the model cannot choose a path or a host);
  * 429 from the claims platform is retried within a small, bounded wait;
  * upstream errors become short structured results, never raw upstream bodies.

Not yet (later steps): full PHI allow-list/scrubbing and caller verification (Step 5),
budgets and the full trace (Step 9). Claim `notes` and provider NPI are deliberately not
passed on: notes are free text from third parties and are untrusted (they can contain
instructions aimed at an AI agent).
"""
from __future__ import annotations

import asyncio
import functools
import hmac
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx

from app.config import Settings
from app.gateway.phi import sanitize_result

log =logging.getLogger("uvicorn.error")

CLAIM_ID_RE = re.compile(r"^C\d{6,12}$")
MAX_CURSOR = 100_000
PAGE_SIZE = 5


@dataclass(frozen=True)
class SessionContext:
    tenant: str
    member_id: str
    conversation_id: str


SSN_LAST4_ARG_RE = re.compile(r"^\d{4}$")
DOB_ARG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _err(code: str, message: str) -> dict:
    return {"error": code, "message": message}


def _filtered(fn):
    """Scrub free text in a tool result for SSN/phone/email/DOB patterns (when PHI_FILTER_ENABLED)."""

    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        result = await fn(self, *args, **kwargs)
        return sanitize_result(result) if self.settings.phi_filter_enabled else result

    return wrapper


class ToolGateway:
    def __init__(self, ctx: SessionContext, settings: Settings, client: httpx.AsyncClient | None = None):
        self.ctx = ctx
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._verify_calls = 0

    async def __aenter__(self) -> "ToolGateway":
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.member_api_base, timeout=self.settings.http_timeout_seconds
            )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    # ---- low-level call -------------------------------------------------
    async def _get(self, tool: str, path: str, params: dict | None = None) -> tuple[int, dict | None]:
        """GET with the session tenant's token. Returns (status, json or None). Retries 429 (bounded)."""
        token = self.settings.tenant_tokens.get(self.ctx.tenant, "")
        if not token:
            log.warning("gateway tool=%s decision=denied reason=no_tenant_token", tool)
            return 0, None
        headers = {"Authorization": f"Bearer {token}"}
        started = time.perf_counter()
        status = 0
        for attempt in range(self.settings.max_api_retries + 1):
            try:
                resp = await self._client.get(path, params=params, headers=headers)
            except httpx.HTTPError:
                status = 0
                break
            status = resp.status_code
            if status == 429 and attempt < self.settings.max_api_retries:
                try:
                    wait = float(resp.headers.get("Retry-After", "1"))
                except ValueError:
                    wait = 1.0
                if wait > self.settings.retry_after_max_seconds:
                    break  # would blow the latency budget: give up, report "busy"
                await asyncio.sleep(max(wait, 0.0))
                continue
            break
        log.info(
            "gateway tool=%s conv=%s status=%s ms=%d",
            tool, self.ctx.conversation_id, status, int((time.perf_counter() - started) * 1000),
        )
        if status == 200:
            try:
                return status, resp.json()
            except ValueError:
                return 0, None
        return status, None

    @staticmethod
    def _failure(status: int, what: str) -> dict:
        if status == 404:
            return _err("not_found", f"{what} was not found for this plan.")
        if status == 429:
            return _err("system_busy", "The claims system is busy. Ask the rep to try again in a moment.")
        return _err("unavailable", f"{what} is temporarily unavailable.")

    # ---- projections (minimum data the model needs) ------------------------
    @staticmethod
    def _claim_view(c: dict) -> dict:
        provider = c.get("provider") or {}
        return {
            "claim_id": c.get("claim_id"),
            "service_date": c.get("service_date"),
            "received_date": c.get("received_date"),
            "processed_date": c.get("processed_date"),
            "status": c.get("status"),
            "provider_name": provider.get("name"),
            "place_of_service": c.get("place_of_service"),
            "service": c.get("service"),
            "billed_amount": c.get("billed_amount"),
            "allowed_amount": c.get("allowed_amount"),
            "plan_paid": c.get("plan_paid"),
            "member_responsibility": c.get("member_responsibility"),
            "denial": c.get("denial"),
        }

    # ---- tools ------------------------------------------------------------
    def _member_path(self, suffix: str = "") -> str:
        return f"/tenants/{self.ctx.tenant}/members/{self.ctx.member_id}{suffix}"

    @_filtered
    async def get_member_summary(self) -> dict:
        status, data = await self._get("get_member_summary", self._member_path())
        if data is None:
            return self._failure(status, "The member")
        return {
            "product_code": data.get("product_code"),
            "plan_name": data.get("plan_name"),
            "network_id": data.get("network_id"),
            "coverage_effective": data.get("coverage_effective"),
            "coverage_termination": data.get("coverage_termination"),
        }

    @_filtered
    async def get_benefits(self) -> dict:
        status, data = await self._get("get_benefits", self._member_path("/benefits"))
        if data is None:
            return self._failure(status, "The member's benefits record")
        return {
            "product_code": data.get("product_code"),
            "plan_name": data.get("plan_name"),
            "network_id": data.get("network_id"),
            "coverage_status": data.get("coverage_status"),
            "coverage_effective": data.get("coverage_effective"),
            "coverage_termination": data.get("coverage_termination"),
            "accumulators": data.get("accumulators"),
        }

    @_filtered
    async def list_claims(self, cursor: int = 0) -> dict:
        if not isinstance(cursor, int) or isinstance(cursor, bool) or not 0 <= cursor <= MAX_CURSOR:
            return _err("invalid_argument", "cursor must be a whole number from the previous result.")
        status, data = await self._get(
            "list_claims", self._member_path("/claims"), {"page_size": PAGE_SIZE, "cursor": cursor}
        )
        if data is None:
            return self._failure(status, "The member's claims")
        claims = [
            self._claim_view(c)
            for c in data.get("data", [])
            if c.get("member_id") == self.ctx.member_id  # defence in depth
        ]
        next_cursor = None
        nxt = data.get("next")
        if nxt:  # rebuild from the number only; never follow an upstream path as given
            try:
                next_cursor = int(parse_qs(urlparse(nxt).query)["cursor"][0])
            except (KeyError, ValueError, IndexError):
                next_cursor = None
        return {"claims": claims, "next_cursor": next_cursor, "order": "newest service date first"}

    async def verify_caller(self, ssn_last4: str | None = None, dob: str | None = None) -> dict:
        """Compare what the caller said with the record, inside the gateway.

        The raw record (with SSN and DOB) is read here and never leaves this method. The model only
        gets {"match": true|false, "checked": [...]}: no values, and no hint which field differed.
        """
        ssn_last4 = (ssn_last4 or "").strip() or None
        dob = (dob or "").strip() or None
        if ssn_last4 is None and dob is None:
            return _err("invalid_argument", "Provide ssn_last4 and/or dob (YYYY-MM-DD) as the caller stated them.")
        if (ssn_last4 and not SSN_LAST4_ARG_RE.fullmatch(ssn_last4)) or (dob and not DOB_ARG_RE.fullmatch(dob)):
            return _err("invalid_argument", "ssn_last4 must be 4 digits and dob must be YYYY-MM-DD.")
        self._verify_calls += 1
        if self._verify_calls > self.settings.max_verify_attempts:
            log.warning("gateway tool=verify_caller conv=%s decision=denied reason=too_many_attempts",
                        self.ctx.conversation_id)
            return _err("too_many_attempts", "Too many verification attempts. Ask the rep to follow the manual process.")
        status, data = await self._get("verify_caller", self._member_path())
        if data is None:
            return self._failure(status, "The member")
        checks = []
        if ssn_last4:
            actual = re.sub(r"\D", "", str(data.get("ssn") or ""))[-4:]
            checks.append(bool(actual) and hmac.compare_digest(actual.encode(), ssn_last4.encode()))
        if dob:
            checks.append(hmac.compare_digest(str(data.get("dob") or "").encode(), dob.encode()))
        checked = [n for n, v in (("ssn_last4", ssn_last4), ("dob", dob)) if v]
        log.info("gateway tool=verify_caller conv=%s checked=%s", self.ctx.conversation_id, ",".join(checked))
        return {"match": all(checks), "checked": checked}

    @_filtered
    async def get_claim(self, claim_id: str) -> dict:
        if not isinstance(claim_id, str) or not CLAIM_ID_RE.fullmatch(claim_id.strip()):
            return _err("invalid_argument", "claim_id must look like C2639555547.")
        claim_id = claim_id.strip()
        status, data = await self._get("get_claim", f"/tenants/{self.ctx.tenant}/claims/{claim_id}")
        if data is None:
            return self._failure(status, "The claim")
        # The API scopes claims to the tenant only. Enforce the member here.
        if data.get("member_id") != self.ctx.member_id:
            log.warning(
                "gateway tool=get_claim conv=%s decision=denied reason=claim_not_owned_by_session_member",
                self.ctx.conversation_id,
            )
            return self._failure(404, "The claim")  # same answer as a missing claim: no hint it exists
        return self._claim_view(data)
