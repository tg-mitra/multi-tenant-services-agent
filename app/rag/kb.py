"""Plan-document knowledge base (Step 6): load, chunk and search the Markdown files in kb/.

Isolation rules (NFR-4), enforced here in code and never left to the prompt:
  * a chunk's tenant, plan year and product come from the document's FRONT-MATTER, not from the folder
    it sits in (one pack file is filed under the wrong tenant folder);
  * a search sees only chunks of the session tenant, the member's product (or product ALL) and the
    requested plan year. Product documents need the exact year. Documents for ALL products fall back
    to the latest earlier year (each passage carries its own plan year so the answer can say so).

Chunks are table rows ("Specialist visit: In-network member cost: $40 copay; ...") and text lines, so a
passage is precise. Ranking is BM25 over the allowed chunks only.
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("uvicorn.error")

FRONT_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
TOKEN_RE = re.compile(r"[a-z0-9]+")
MONEY_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")
REQUIRED_META = ("tenant", "plan_year", "product_code", "product")
STOP = {"the", "a", "an", "of", "for", "and", "or", "to", "is", "are", "what", "does", "do", "how", "much",
        "member", "this", "that", "in", "on", "at", "with", "pay", "cost", "per", "after", "by", "be"}
# Small synonym list: reps say "ER", "PCP", "video visit" ...
SYNONYMS = {
    "er": ["emergency", "room"], "pcp": ["primary", "care"], "video": ["telehealth"], "virtual": ["telehealth"],
    "telemedicine": ["telehealth"], "doctor": ["primary", "specialist"], "mri": ["advanced", "imaging"],
    "ct": ["advanced", "imaging"], "pet": ["advanced", "imaging"], "scan": ["advanced", "imaging"],
    "pt": ["physical", "therapy"], "therapy": ["physical", "behavioral"], "counseling": ["behavioral"],
    "mental": ["behavioral"], "drug": ["drugs"], "prescription": ["drugs", "generic"], "rx": ["drugs"],
    "pharmacy": ["drugs"], "oop": ["out", "pocket", "maximum"], "oopm": ["out", "pocket", "maximum"],
    "copay": ["copay"], "hospital": ["inpatient"], "surgery": ["outpatient", "surgery"],
}


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    tenant: str
    plan_year: int
    product_code: str
    product: str
    title: str
    label: str  # table row label, or the section heading for text lines
    kind: str  # "row" | "text"
    text: str
    headers: tuple[str, ...] = ()
    cells: tuple[str, ...] = ()


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOP]


def _expand(tokens: list[str]) -> list[str]:
    out = list(tokens)
    for t in tokens:
        out.extend(SYNONYMS.get(t, []))
    return out


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c) and any(cells)


def parse_document(path: Path) -> tuple[list[Chunk], str | None]:
    """Return (chunks, warning or None). A document without valid front-matter is skipped."""
    raw = path.read_text(encoding="utf-8")
    m = FRONT_RE.match(raw)
    if not m:
        return [], f"{path.name}: no front-matter, skipped"
    meta = dict(re.findall(r"^(\w+):\s*(.*?)\s*$", m.group(1), re.M))
    missing = [k for k in REQUIRED_META if k not in meta]
    if missing:
        return [], f"{path.name}: front-matter missing {missing}, skipped"
    try:
        year = int(meta["plan_year"])
    except ValueError:
        return [], f"{path.name}: plan_year is not a number, skipped"

    body = raw[m.end():]
    title = next((ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")), path.stem)
    tag = f"{title} [{meta['product']}, plan year {year}]"
    base = dict(doc_id=path.name, tenant=meta["tenant"], plan_year=year,
                product_code=meta["product_code"], product=meta["product"], title=title)
    chunks: list[Chunk] = []
    heading = title
    headers: list[str] | None = None
    for line in body.splitlines():
        s = line.strip()
        if not s:
            headers = None
            continue
        if s.startswith("#"):
            heading = s.lstrip("#").strip()
            headers = None
            continue
        if s.startswith("|"):
            cells = _split_row(s)
            if _is_separator(cells):
                continue
            if headers is None:
                headers = cells
                continue
            label = cells[0]
            detail = "; ".join(f"{h}: {c}" for h, c in zip(headers[1:], cells[1:]))
            chunks.append(Chunk(label=label, kind="row", text=f"{tag} {label}: {detail}",
                                headers=tuple(headers), cells=tuple(cells), **base))
            continue
        headers = None
        chunks.append(Chunk(label=heading, kind="text", text=f"{tag} {s.lstrip('-* ').strip()}", **base))

    warning = None
    folder = path.parent.name
    if folder in ("plan_a", "plan_b") and meta["tenant"] != folder:
        warning = (f"{path.name}: filed under {folder}/ but its front-matter says tenant={meta['tenant']}; "
                   f"it is used ONLY for {meta['tenant']}")
    return chunks, warning


class KbLookupError(LookupError):
    """A required value (for example a plan limit) is not in the allowed documents."""


class KnowledgeBase:
    def __init__(self, chunks: list[Chunk], warnings: list[str] | None = None):
        self.chunks = chunks
        self.warnings = warnings or []
        self._tokens = [Counter(_expand(tokenize(c.text)) + tokenize(c.label) * 2) for c in chunks]

    # ---- filtering (the isolation boundary) ---------------------------------
    def candidates(self, tenant: str, product_code: str, plan_year: int) -> list[int]:
        """Indexes of chunks this tenant/product/year may see."""
        idx = []
        by_all: dict[str, list[int]] = {}
        for i, c in enumerate(self.chunks):
            if c.tenant != tenant:
                continue
            if c.product_code == product_code and c.plan_year == plan_year:
                idx.append(i)
            elif c.product_code == "ALL" and c.plan_year <= plan_year:
                by_all.setdefault(re.sub(r"[_-]?\d{4}", "", c.doc_id), []).append(i)
        for group in by_all.values():  # per document: the requested year, else the latest earlier one
            best = max(self.chunks[i].plan_year for i in group)
            idx.extend(i for i in group if self.chunks[i].plan_year == best)
        return idx

    # ---- search ---------------------------------------------------------------
    def search(self, tenant: str, product_code: str, plan_year: int, query: str, k: int = 5) -> list[Chunk]:
        cand = self.candidates(tenant, product_code, plan_year)
        q = Counter(_expand(tokenize(query)))
        if not cand or not q:
            return []
        n = len(cand)
        lengths = {i: sum(self._tokens[i].values()) or 1 for i in cand}
        avg = sum(lengths.values()) / n
        df = {t: sum(1 for i in cand if t in self._tokens[i]) for t in q}
        scored = []
        for i in cand:
            score = 0.0
            for t in q:
                tf = self._tokens[i].get(t, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * lengths[i] / avg))
            if score > 0:
                scored.append((score, i))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [self.chunks[i] for _, i in scored[:k]]

    # ---- exact lookups for calculators ---------------------------------------------
    def _row(self, tenant: str, product_code: str, plan_year: int, label_prefix: str) -> Chunk:
        for i in self.candidates(tenant, product_code, plan_year):
            c = self.chunks[i]
            if c.kind == "row" and c.label.lower().startswith(label_prefix.lower()):
                return c
        raise KbLookupError(f"'{label_prefix}' not found for {tenant}/{product_code}/{plan_year}")

    @staticmethod
    def _money_pair(cell: str) -> tuple[Decimal, Decimal | None]:
        vals = [Decimal(v.replace(",", "")) for v in MONEY_RE.findall(cell)]
        if not vals:
            raise KbLookupError(f"no amount in '{cell}'")
        return vals[0], (vals[1] if len(vals) > 1 else None)

    def plan_limits(self, tenant: str, product_code: str, plan_year: int) -> dict:
        """In-network individual/family deductible and out-of-pocket maximum and the PT visit limit."""
        ded = self._row(tenant, product_code, plan_year, "Deductible")
        oop = self._row(tenant, product_code, plan_year, "Out-of-pocket maximum")
        pt = self._row(tenant, product_code, plan_year, "Physical therapy visit limit")
        d_ind, d_fam = self._money_pair(ded.cells[1])
        o_ind, o_fam = self._money_pair(oop.cells[1])
        m = re.search(r"(\d+)\s+visits", pt.cells[1])
        if not m:
            raise KbLookupError("no visit limit")
        return {
            "deductible_individual": d_ind, "deductible_family": d_fam,
            "oop_individual": o_ind, "oop_family": o_fam,
            "pt_visit_limit": int(m.group(1)),
            "source": ded.doc_id, "product": ded.product,
        }

    def all_products_row(self, tenant: str, label_prefix: str, plan_year: int) -> Chunk | None:
        """A table row of this tenant's documents that apply to every product (member services guide, claim
        codes), newest plan year not after `plan_year`. Used where no product is needed (denial codes)."""
        best: Chunk | None = None
        for c in self.chunks:
            if (c.tenant == tenant and c.product_code == "ALL" and c.kind == "row" and c.plan_year <= plan_year
                    and c.label.lower().startswith(label_prefix.lower())):
                if best is None or c.plan_year > best.plan_year:
                    best = c
        return best

    def rule_days(self, tenant: str, product_code: str, plan_year: int, label_prefix: str) -> tuple[int, str]:
        """Number of calendar days in a member-services-guide rule, plus the document it came from."""
        c = self._row(tenant, product_code, plan_year, label_prefix)
        m = re.search(r"(\d+)\s+calendar days", c.text)
        if not m:
            raise KbLookupError(f"no day count in rule '{label_prefix}'")
        return int(m.group(1)), c.doc_id


def load_kb(kb_dir: Path) -> KnowledgeBase:
    chunks: list[Chunk] = []
    warnings: list[str] = []
    for path in sorted(Path(kb_dir).rglob("*.md")):
        got, warn = parse_document(path)
        chunks.extend(got)
        if warn:
            warnings.append(warn)
    for w in warnings:
        log.warning("kb: %s", w)
    return KnowledgeBase(chunks, warnings)


@lru_cache(maxsize=4)
def get_kb(kb_dir: str) -> KnowledgeBase:
    return load_kb(Path(kb_dir))
