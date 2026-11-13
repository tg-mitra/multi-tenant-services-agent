"""Development helper: list tenants and member ids so you know what to put in /conversations.

The mock API has no "list members" endpoint (search needs a filter), so this reads the pack files
directly. That is acceptable for a developer aid only; the agent itself never reads these files and
the route is off unless DEV_ENDPOINTS_ENABLED=true.

Privacy: only member_id, product and plan info and coverage status are returned. No names, DOB, SSN,
contact details or tokens, even though the files contain them.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings


class PackNotFound(RuntimeError):
    """The pack files could not be found."""


def _load(path: Path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise PackNotFound(str(path.name)) from exc


def _eval_cases_by_member(pack: Path) -> dict[tuple[str, str], list[dict]]:
    """(tenant, member_id) -> eval cases that use that member. Optional: missing file = none."""
    try:
        cases = _load(pack / "eval" / "conversations.json").get("cases", [])
    except PackNotFound:
        return {}
    out: dict[tuple[str, str], list[dict]] = {}
    for c in cases:
        out.setdefault((c["tenant"], c["member_id"]), []).append(
            {"id": c["id"], "behaviour": c["expected"]["behaviour"]}
        )
    return out


def list_tenants_and_members(settings: Settings, tenant: str | None = None) -> dict:
    """Return {"as_of", "tenants": [{tenant, client, products, member_count, members: [...]}]}."""
    data_dir = settings.pack_dir / "mock_services" / "data"
    cfg = _load(data_dir / "tenants.json")
    eval_map = _eval_cases_by_member(settings.pack_dir)
    as_of = settings.as_of_date

    tenants = []
    for tid, info in cfg["tenants"].items():
        if tenant and tid != tenant:
            continue
        members = []
        for m in _load(data_dir / tid / "members.json"):
            term = m.get("coverage_termination")
            members.append(
                {
                    "member_id": m["member_id"],
                    "product_code": m["product_code"],
                    "plan_name": m["plan_name"],
                    "coverage_status": "TERMINATED" if term and term < as_of else "ACTIVE",
                    "coverage_termination": term,
                    "eval_cases": eval_map.get((tid, m["member_id"]), []),
                }
            )
        tenants.append(
            {
                "tenant": tid,
                "client": info.get("name"),
                "products": [
                    {"product_code": p["product_code"], "product": p["product"]} for p in info.get("products", [])
                ],
                "member_count": len(members),
                "members": members,
            }
        )
    return {"as_of": as_of, "tenants": tenants}
