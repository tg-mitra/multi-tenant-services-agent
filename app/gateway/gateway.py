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

from app.calc import deadline_status, fmt_money, money_line, parse_date
from app.config import Settings
from app.gateway.phi import redact_text, sanitize_result
from app.grievance import CATEGORIES, MAX_DESCRIPTION, new_pending, pending_is_consistent
from app.rag.kb import KbLookupError, KnowledgeBase, get_kb
from app.store.jsonfile import IdempotencyStore

log =logging.getLogger("uvicorn.error")

CLAIM_ID_RE = re.compile(r"^C\d{6,12}$")
GRIEVANCE_ID_RE = re.compile(r"^G\d{2}-\d{6}$")
ZIP_RE = re.compile(r"^\d{5}$")
MAX_PROVIDERS = 10
# Specialties the directory knows (it needs an exact name). Reps say "dermatologist", "heart doctor"...
SPECIALTIES = ("Behavioral Health", "Cardiology", "Dermatology", "Endocrinology", "Family Medicine",
               "Gastroenterology", "Orthopedic Surgery", "Physical Therapy")
SPECIALTY_ALIASES = {
    "dermatologist": "Dermatology", "skin": "Dermatology", "cardiologist": "Cardiology", "heart": "Cardiology",
    "endocrinologist": "Endocrinology", "diabetes": "Endocrinology", "gastroenterologist": "Gastroenterology",
    "gi": "Gastroenterology", "orthopedic": "Orthopedic Surgery", "orthopedics": "Orthopedic Surgery",
    "orthopedist": "Orthopedic Surgery", "orthopedic surgeon": "Orthopedic Surgery", "physical therapist": "Physical Therapy",
    "pt": "Physical Therapy", "physiotherapy": "Physical Therapy", "behavioral": "Behavioral Health",
    "mental health": "Behavioral Health", "therapist": "Behavioral Health", "psychologist": "Behavioral Health",
    "psychiatrist": "Behavioral Health", "counseling": "Behavioral Health", "primary care": "Family Medicine",
    "family doctor": "Family Medicine", "family practice": "Family Medicine", "pcp": "Family Medicine",
}


def normalize_specialty(value) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    for s in SPECIALTIES:
        if v == s.lower():
            return s
    return SPECIALTY_ALIASES.get(v)
MAX_CURSOR = 100_000
PAGE_SIZE = 5


@dataclass(frozen=True)
class SessionContext:
    tenant: str
    member_id: str
    conversation_id: str


_grievance_locks: dict[tuple[str, str, str], asyncio.Lock] = {}  # one writer per (tenant, member, key)
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
    def __init__(
        self,
        ctx: SessionContext,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        kb: KnowledgeBase | None = None,
        idem_store: IdempotencyStore | None = None,
    ):
        self.ctx = ctx
        self.settings = settings
        self._kb_obj = kb
        self.idem = idem_store
        self.proposal: dict | None = None  # set by propose_grievance; persisted by the API layer
        self._benefits: dict | None = None  # benefits record, fetched at most once per rep message
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

    async def _load_benefits(self) -> tuple[int, dict | None]:
        """Benefits record (product, coverage status, accumulators), fetched once and reused."""
        if self._benefits is None:
            status, data = await self._get("get_benefits", self._member_path("/benefits"))
            if data is None:
                return status, None
            self._benefits = data
        return 200, self._benefits

    @_filtered
    async def get_benefits(self) -> dict:
        status, data = await self._load_benefits()
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

    # ---- plan documents and calculators (Step 6) ---------------------------------------
    def _kb(self) -> KnowledgeBase:
        if self._kb_obj is None:
            self._kb_obj = get_kb(str(self.settings.kb_dir))
        return self._kb_obj

    def _year(self, plan_year) -> int | None:
        if plan_year is None:
            return int(self.settings.as_of_date[:4])
        if isinstance(plan_year, bool) or not isinstance(plan_year, int) or not 2000 <= plan_year <= 2100:
            return None
        return plan_year

    @staticmethod
    def _coverage_note(ben: dict) -> dict:
        if ben.get("coverage_status") == "TERMINATED":
            return {"coverage_note": f"Coverage is NOT active: it ended on {ben.get('coverage_termination')}. "
                                     "Do not quote costs as if the member were covered."}
        return {}

    @_filtered
    async def search_plan_documents(self, query: str, plan_year: int | None = None) -> dict:
        """Passages from this member's own plan documents (tenant + product + plan year enforced here)."""
        year = self._year(plan_year)
        if year is None:
            return _err("invalid_argument", "plan_year must be a year such as 2026 or 2027.")
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            return _err("invalid_argument", "query must be 1 to 200 characters.")
        status, ben = await self._load_benefits()
        if ben is None:
            return self._failure(status, "The member's plan")
        try:
            passages = self._kb().search(self.ctx.tenant, ben["product_code"], year, query, self.settings.rag_top_k)
        except Exception:
            log.error("gateway tool=search_plan_documents conv=%s kb_error", self.ctx.conversation_id)
            return _err("unavailable", "The plan documents are temporarily unavailable.")
        result = {
            "plan_year": year,
            "product": ben.get("plan_name"),
            "coverage_status": ben.get("coverage_status"),
            "coverage_termination": ben.get("coverage_termination"),
            "passages": [{"source": c.doc_id, "plan_year": c.plan_year, "text": c.text} for c in passages],
            **self._coverage_note(ben),
        }
        if not passages:
            result["note"] = ("No matching passage in this member's plan documents for that plan year. "
                              "Say the information is not available; do not guess.")
        log.info("gateway tool=search_plan_documents conv=%s year=%s passages=%d",
                 self.ctx.conversation_id, year, len(passages))
        return result

    @_filtered
    async def get_plan_status(self) -> dict:
        """Deductible / out-of-pocket / PT visits: plan limit (documents) minus amount used (API), in code."""
        status, ben = await self._load_benefits()
        if ben is None:
            return self._failure(status, "The member's benefits record")
        acc = ben.get("accumulators") or {}
        year = acc.get("plan_year")
        if not acc or year is None:
            return _err("not_available", "No accumulators are on file for this member.")
        try:
            lim = self._kb().plan_limits(self.ctx.tenant, ben["product_code"], int(year))
        except KbLookupError:
            return _err("not_available", "The plan limits for this plan year are not in the plan documents.")
        used_pt = int(acc.get("physical_therapy_visits_used") or 0)
        family = {}
        if lim["deductible_family"] is not None:
            family["deductible"] = fmt_money(lim["deductible_family"])
        if lim["oop_family"] is not None:
            family["out_of_pocket"] = fmt_money(lim["oop_family"])
        return {
            "plan_year": year,
            "accumulators_as_of": acc.get("as_of"),
            "product": lim["product"],
            "coverage_status": ben.get("coverage_status"),
            "scope": "in-network, individual",
            "deductible": money_line(lim["deductible_individual"], acc.get("deductible_met_individual", 0)),
            "out_of_pocket": money_line(lim["oop_individual"], acc.get("out_of_pocket_met_individual", 0)),
            "physical_therapy_visits": {
                "limit": lim["pt_visit_limit"],
                "used": used_pt,
                "remaining": max(0, lim["pt_visit_limit"] - used_pt),
            },
            "family_limits_for_reference": family,
            "source": lim["source"],
            **self._coverage_note(ben),
        }

    @_filtered
    async def check_appeal_window(self, claim_id: str) -> dict:
        """Appeal deadline = denial notice date (claim processed date) + the plan's window (from the guide)."""
        claim = await self.get_claim(claim_id)  # validates the id and enforces claim ownership
        if "error" in claim:
            return claim
        if claim.get("status") != "DENIED":
            return {"claim_id": claim.get("claim_id"), "status": claim.get("status"), "appeal_applicable": False,
                    "message": "Only denied claims can be appealed."}
        notice = claim.get("processed_date")
        if not notice:
            return _err("not_available", "The denial notice date (processed date) is not on the claim.")
        status, ben = await self._load_benefits()
        if ben is None:
            return self._failure(status, "The member's plan")
        try:
            days, source = self._kb().rule_days(
                self.ctx.tenant, ben["product_code"], int(self.settings.as_of_date[:4]), "Claim appeal filing window"
            )
            res = deadline_status(notice, days, self.settings.as_of_date)
        except (KbLookupError, ValueError):
            return _err("not_available", "The appeal window for this plan is not available.")
        return {
            "claim_id": claim["claim_id"], "status": "DENIED", "denial": claim.get("denial"),
            "denial_notice_date": notice, "appeal_window_days": days, "appeal_deadline": res["deadline"],
            "as_of": res["as_of"], "within_appeal_window": not res["deadline_passed"],
            "days_remaining": res["days_remaining"], "days_overdue": res["days_overdue"], "source": source,
        }

    # ---- grievances (read only; filing is Step 7) -----------------------------------------
    async def _grievances(self) -> tuple[int, list[dict] | None]:
        status, data = await self._get("list_grievances", self._member_path("/grievances"))
        if data is None:
            return status, None
        return 200, [g for g in data.get("data", []) if g.get("member_id") == self.ctx.member_id]

    @staticmethod
    def _grievance_view(g: dict) -> dict:  # the free-text description is not passed on
        return {
            "grievance_id": g.get("grievance_id"), "category": g.get("category"), "status": g.get("status"),
            "received_date": g.get("received_date"), "resolved_date": g.get("resolved_date"),
            "expedite_requested": g.get("expedite_requested"),
        }

    @_filtered
    async def list_grievances(self) -> dict:
        status, items = await self._grievances()
        if items is None:
            return self._failure(status, "The member's grievances")
        return {"grievances": [self._grievance_view(g) for g in items]}

    @_filtered
    async def check_grievance_deadline(self, grievance_id: str) -> dict:
        """Resolution deadline = received date + the plan's timeframe (from the guide); tenant-specific."""
        if not isinstance(grievance_id, str) or not GRIEVANCE_ID_RE.fullmatch(grievance_id.strip()):
            return _err("invalid_argument", "grievance_id must look like G26-600230.")
        grievance_id = grievance_id.strip()
        status, items = await self._grievances()
        if items is None:
            return self._failure(status, "The member's grievances")
        g = next((x for x in items if x.get("grievance_id") == grievance_id), None)
        if g is None:
            return self._failure(404, "The grievance")
        status, ben = await self._load_benefits()
        if ben is None:
            return self._failure(status, "The member's plan")
        try:
            days, source = self._kb().rule_days(
                self.ctx.tenant, ben["product_code"], int(self.settings.as_of_date[:4]), "Grievance resolution timeframe"
            )
            res = deadline_status(g["received_date"], days, self.settings.as_of_date)
        except (KbLookupError, ValueError, KeyError):
            return _err("not_available", "The resolution timeframe for this plan is not available.")
        resolved = g.get("resolved_date")
        past = (parse_date(resolved) > parse_date(res["deadline"])) if resolved else res["deadline_passed"]
        return {
            "grievance_id": grievance_id, "status": g.get("status"), "received_date": g["received_date"],
            "resolved_date": resolved, "resolution_timeframe_days": days, "resolution_deadline": res["deadline"],
            "as_of": res["as_of"], "past_resolution_deadline": past,
            "days_overdue": res["days_overdue"] if not resolved else None, "source": source,
        }

    # ---- filing a grievance (Step 7) --------------------------------------------------------
    # The model can only PROPOSE. file_grievance() is called by the API layer after the rep's own
    # next message was classified as a confirmation; it is not exposed as a tool.
    @_filtered
    async def propose_grievance(self, category: str, description: str, expedite_requested: bool = False) -> dict:
        """Prepare a pending proposal for the rep to confirm. Never calls the write API."""
        if category not in CATEGORIES:
            return _err("invalid_argument", f"category must be one of {', '.join(CATEGORIES)}.")
        if not isinstance(description, str) or not description.strip():
            return _err("invalid_argument", "description is required.")
        if not isinstance(expedite_requested, bool):
            return _err("invalid_argument", "expedite_requested must be true or false.")
        text = description.strip()
        if self.settings.phi_filter_enabled:
            text = redact_text(text)[0]  # no identifiers in what is filed or shown
        if len(text) > MAX_DESCRIPTION:
            return _err("invalid_argument", f"description is too long (max {MAX_DESCRIPTION} characters).")
        duplicates: list[str] = []
        status, items = await self._grievances()  # best effort: a failure here does not block the proposal
        if items:
            duplicates = [g["grievance_id"] for g in items
                          if g.get("category") == category and g.get("status") not in ("RESOLVED", "CLOSED")]
        self.proposal = new_pending(self.ctx.tenant, self.ctx.member_id, category, text, expedite_requested,
                                    self.settings.pending_ttl_seconds, duplicates)
        return {
            "status": "awaiting_rep_confirmation",
            "category": category, "description": text, "expedite_requested": expedite_requested,
            "possible_duplicates": duplicates,
            "instruction": ("Nothing has been filed. The system will now show the rep a read-back and ask them "
                            "to confirm or cancel. Reply with ONE short sentence and do not say it was filed."),
        }

    def _auth_headers(self) -> dict | None:
        token = self.settings.tenant_tokens.get(self.ctx.tenant, "")
        return {"Authorization": f"Bearer {token}"} if token else None

    async def _find_saved_grievance(self, pending: dict) -> dict | None:
        """After retries are exhausted: was it saved anyway? (the mock can answer 503 after saving)"""
        status, items = await self._grievances()
        if not items:
            return None
        matches = [g for g in items
                   if g.get("category") == pending["category"]
                   and (g.get("description") or "").strip() == pending["description"]
                   and bool(g.get("expedite_requested")) == bool(pending["expedite_requested"])
                   and g.get("received_date") == self.settings.as_of_date]
        return matches[-1] if matches else None

    @staticmethod
    def _grievance_summary(g: dict) -> dict:
        return {k: g.get(k) for k in ("grievance_id", "status", "received_date", "category")}

    async def file_grievance(self, pending: dict) -> dict:
        """Write the confirmed grievance exactly once.

        Returns {"state": "filed" | "already_filed" | "unconfirmed" | "failed", ...}. Safe to call again
        for the same pending action: the idempotency key and the exact request bytes never change.
        """
        if self.idem is None:
            return {"state": "failed", "reason": "no_idempotency_store"}
        if not pending_is_consistent(pending, self.ctx.tenant, self.ctx.member_id):
            log.error("gateway tool=file_grievance conv=%s decision=denied reason=inconsistent_pending",
                      self.ctx.conversation_id)
            return {"state": "failed", "reason": "inconsistent_pending"}
        headers = self._auth_headers()
        if headers is None:
            return {"state": "failed", "reason": "no_tenant_token"}
        key, body = pending["idempotency_key"], pending["body"]
        tenant, member = self.ctx.tenant, self.ctx.member_id

        lock = _grievance_locks.setdefault((tenant, member, key), asyncio.Lock())
        async with lock:
            record = self.idem.get(tenant, key)
            if record and record.get("status") == "done":
                return {"state": "already_filed", "grievance": record["grievance"]}
            # Written BEFORE the POST: if we crash or time out, the key survives and is reused.
            self.idem.put(tenant, key, {"status": "in_flight", "member_id": member,
                                        "conversation_id": self.ctx.conversation_id})
            post_headers = {**headers, "Idempotency-Key": key, "Content-Type": "application/json"}
            path = f"/tenants/{tenant}/members/{member}/grievances"
            attempts = 0
            for attempt in range(self.settings.max_grievance_retries + 1):
                attempts += 1
                started = time.perf_counter()
                try:
                    resp = await self._client.post(path, content=body.encode("utf-8"), headers=post_headers)
                    status = resp.status_code
                except httpx.HTTPError:
                    resp, status = None, 0
                log.info("gateway tool=file_grievance conv=%s key=%s attempt=%d status=%s ms=%d",
                         self.ctx.conversation_id, key[:10], attempts, status,
                         int((time.perf_counter() - started) * 1000))
                if status in (200, 201):
                    try:
                        grievance = resp.json()
                    except ValueError:
                        break  # cannot read the answer: treat as unconfirmed and verify below
                    summary = self._grievance_summary(grievance)
                    self.idem.put(tenant, key, {"status": "done", "member_id": member, "grievance": summary})
                    return {"state": "filed", "grievance": summary, "attempts": attempts}
                if status in (0, 429, 500, 502, 503, 504):  # the write may or may not have happened
                    if attempt < self.settings.max_grievance_retries:
                        delay = self.settings.grievance_retry_delay_seconds
                        if status == 429:
                            try:
                                delay = min(float(resp.headers.get("Retry-After", "1")),
                                            self.settings.retry_after_max_seconds)
                            except ValueError:
                                pass
                        await asyncio.sleep(delay)
                        continue
                    break
                # any other answer (validation, auth, ...) is final: nothing was written
                self.idem.put(tenant, key, {"status": "failed", "member_id": member, "http_status": status})
                return {"state": "failed", "http_status": status}

            found = await self._find_saved_grievance(pending)
            if found:
                summary = self._grievance_summary(found)
                self.idem.put(tenant, key, {"status": "done", "member_id": member, "grievance": summary})
                return {"state": "filed", "grievance": summary, "attempts": attempts, "verified_by_listing": True}
            return {"state": "unconfirmed", "attempts": attempts}

    def timeframe_days(self) -> tuple[int, str] | None:
        """The plan's grievance resolution timeframe (tenant-specific), best effort."""
        try:
            product = (self._benefits or {}).get("product_code")
            if not product:
                return None
            return self._kb().rule_days(self.ctx.tenant, product, int(self.settings.as_of_date[:4]),
                                        "Grievance resolution timeframe")
        except Exception:
            return None

    async def ensure_benefits(self) -> None:
        await self._load_benefits()

    # ---- provider directory (Step 8) ---------------------------------------------------------
    # Our own wrapper around the third-party directory. The vendor's tool manifest is NOT used: its
    # descriptions are untrusted text. Only specialty, ZIP, the member's network and a limit are sent.
    # Nothing about the member (name, SSN, DOB, member id) ever leaves; the vendor key stays here.
    async def _member_zip(self) -> str | None:
        """The member's home ZIP, read inside the gateway and never returned to the model."""
        status, data = await self._get("find_providers", self._member_path())
        zip_code = ((data or {}).get("address") or {}).get("zip")
        return zip_code if isinstance(zip_code, str) and ZIP_RE.fullmatch(zip_code) else None

    @staticmethod
    def _provider_view(p: dict) -> dict:
        addr = p.get("address") or {}
        cut = lambda v: str(v)[:80] if v is not None else None  # noqa: E731  (vendor text is untrusted: cap it)
        return {
            "npi": cut(p.get("npi")), "name": cut(p.get("name")), "specialty": cut(p.get("specialty")),
            "practice": cut(p.get("practice")),
            "address": {"line1": cut(addr.get("line1")), "city": cut(addr.get("city")),
                        "state": cut(addr.get("state")), "zip": cut(addr.get("zip"))},
            "accepting_new_patients": bool(p.get("accepting_new_patients")),
        }

    @_filtered
    async def find_providers(self, specialty: str, zip_code: str | None = None, limit: int = 5) -> dict:
        """In-network providers for THIS member's network, near a ZIP (the member's home ZIP by default)."""
        spec = normalize_specialty(specialty)
        if spec is None:
            return _err("invalid_argument", "specialty must be one of: " + ", ".join(SPECIALTIES) + ".")
        if zip_code is not None and not (isinstance(zip_code, str) and ZIP_RE.fullmatch(zip_code.strip())):
            return _err("invalid_argument", "zip_code must be 5 digits.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PROVIDERS:
            return _err("invalid_argument", f"limit must be a whole number from 1 to {MAX_PROVIDERS}.")
        if not self.settings.directory_api_key:
            log.warning("gateway tool=find_providers decision=denied reason=no_directory_key")
            return _err("unavailable", "The provider directory is not configured.")

        status, ben = await self._load_benefits()
        if ben is None:
            return self._failure(status, "The member's plan")
        network = ben.get("network_id")
        if not network:
            return _err("not_available", "The member's network is not on file.")
        area = zip_code.strip() if zip_code else await self._member_zip()
        if not area:
            return _err("not_available", "No ZIP code is on file for the member. Ask the rep for a ZIP code.")

        started = time.perf_counter()
        try:
            resp = await self._client.get(
                f"{self.settings.directory_api_base}/providers",
                params={"specialty": spec, "network_id": network, "zip": area, "limit": 50},  # never "query"
                headers={"X-Api-Key": self.settings.directory_api_key},
            )
            status, data = resp.status_code, (resp.json() if resp.status_code == 200 else None)
        except (httpx.HTTPError, ValueError):
            status, data = 0, None
        log.info("gateway tool=find_providers conv=%s status=%s ms=%d", self.ctx.conversation_id, status,
                 int((time.perf_counter() - started) * 1000))
        if data is None:
            return _err("unavailable", "The provider directory is temporarily unavailable.")

        # The directory does not enforce what we asked for (F-21), so every result is re-checked here.
        kept, dropped = [], 0
        for p in data.get("results", []):
            ok = (
                isinstance(p, dict)
                and str(p.get("specialty", "")).lower() == spec.lower()
                and network in (p.get("networks") or [])
                and str((p.get("address") or {}).get("zip", ""))[:3] == area[:3]
            )
            if ok:
                kept.append(self._provider_view(p))
            else:
                dropped += 1
        if dropped:
            log.warning("gateway tool=find_providers conv=%s decision=filtered dropped=%d", self.ctx.conversation_id, dropped)
        result = {
            "specialty": spec, "network": network,
            "searched_near": f"ZIP area {area[:3]}xx" + (" (the member's home area)" if not zip_code else ""),
            "providers": kept[:limit], "total_found": len(kept),
            **self._coverage_note(ben),
        }
        if not kept:
            result["note"] = "No in-network providers found for that specialty and area. Do not suggest providers from memory."
        return result
