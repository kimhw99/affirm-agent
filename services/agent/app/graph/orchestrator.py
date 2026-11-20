"""Step 2: the orchestrator (intake + routing). Plain code, no LLM.

It decides *which* workers run and *which* lookups they get, from a fixed table. It
never decides what the findings mean. Rules:

- Several flags on one case: their task lists are combined and de-duplicated, so the
  registry is looked up once even when EC-1 and EC-2 both ask for it.
- Inputs are checked before any agent runs: a flag whose required inputs are missing
  goes to ``needs_info`` (cheaper than letting an agent find out).
- No flags, unknown flags, only out-of-scope flags, or a malformed payload go to the
  ``human_queue`` with a reason. Never guess.
- The idempotency key is ``case_id`` + a hash of the input, because a case that comes
  back after ``docs_requested`` keeps its case_id but has new evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from pydantic import ValidationError

from app.shared.name_norm import compare_names, irs_name_control, normalize_name
from kyb_contracts.case_input import CASE_ID, CaseInput, FlagReason
from app.review import FLAG_EDGE_CASE

Route = Literal["reviewed", "needs_info", "human_queue"]

# ---------- The routing table ----------

# Lookups the Records worker can be asked for (official and third-party records).
RECORDS_TASKS = {
    "registry_by_name": "Search the state registry by the applied legal name",
    "registry_by_entity_id": "Fetch the registry entity by its state entity number",
    "dba_filing": "Find the DBA / assumed-name filing and its registered owner",
    "filing_history": "Read the entity's filing history (formation, amendments, name changes)",
    "pending_filings": "Look for pending or very recent filings not yet in the registry",
    "tin_match": "Run the IRS TIN match for the submitted name and TIN pair",
    "name_control": "Work out the IRS name control the IRS likely has on file",
    "owner_kyc": "Read the owner KYC result",
    "commercial_db": "Search commercial business databases",
}

# Things the Evidence worker can be asked to read (merchant evidence).
EVIDENCE_TASKS = {
    "formation_docs": "Read the articles / certificate of formation and good standing",
    "business_license": "Read the business license (holder, status, authority)",
    "dba_certificate": "Read the DBA certificate (trade name, registrant)",
    "bank_account": "Read the bank verification (account holder, status)",
    "website": "Read the website snapshot (legal name, DBA, footer)",
    "ein_letter": "Read the IRS EIN letter (CP 575 / 147C): name, EIN last 4, name control, date",
    "w9": "Read the W-9 (legal name, business name, TIN type)",
}


@dataclass(frozen=True)
class FlagRoute:
    edge_case: str
    records: tuple[str, ...]
    evidence: tuple[str, ...]


ROUTING_TABLE: dict[FlagReason, FlagRoute] = {
    # EC-1: legal name vs DBA, suffix and punctuation variants
    FlagReason.NAME_SIMILAR_MATCH: FlagRoute(
        "EC-1",
        ("registry_by_name", "registry_by_entity_id", "filing_history"),
        ("formation_docs", "business_license", "bank_account", "website"),
    ),
    FlagReason.NAME_UNVERIFIED: FlagRoute(
        "EC-1",
        ("registry_by_name", "registry_by_entity_id", "filing_history", "dba_filing"),
        ("formation_docs", "business_license", "bank_account", "website"),
    ),
    FlagReason.DBA_UNVERIFIED: FlagRoute(
        "EC-1",
        ("registry_by_name", "registry_by_entity_id", "dba_filing", "filing_history"),
        ("dba_certificate", "bank_account", "website", "business_license"),
    ),
    # EC-2: EIN/TIN mismatch and IRS name control
    FlagReason.TIN_MISMATCH: FlagRoute(
        "EC-2",
        ("tin_match", "name_control", "registry_by_entity_id", "filing_history"),
        ("ein_letter", "w9"),
    ),
    FlagReason.TIN_NOT_FOUND: FlagRoute(
        "EC-2",
        ("tin_match", "name_control", "registry_by_entity_id"),
        ("ein_letter", "w9"),
    ),
    # EC-4: newly formed businesses and sole proprietors
    FlagReason.TIN_NOT_ISSUED: FlagRoute(
        "EC-4",
        ("tin_match", "registry_by_entity_id", "pending_filings"),
        ("ein_letter", "formation_docs"),
    ),
    FlagReason.ENTITY_NOT_FOUND: FlagRoute(
        "EC-4",
        ("registry_by_name", "pending_filings", "filing_history", "dba_filing", "owner_kyc", "tin_match"),
        ("formation_docs", "dba_certificate", "bank_account", "business_license", "website", "w9"),
    ),
    FlagReason.NO_COMMERCIAL_RECORD: FlagRoute(
        "EC-4",
        ("commercial_db", "registry_by_name", "owner_kyc"),
        ("bank_account", "business_license", "website"),
    ),
}

OUT_OF_SCOPE: frozenset[FlagReason] = frozenset(set(FlagReason) - set(FLAG_EDGE_CASE))
assert set(ROUTING_TABLE) == set(FLAG_EDGE_CASE), "every in-scope flag needs a route"


# ---------- Result ----------

@dataclass
class AutoResolved:
    flag: FlagReason
    reason: str


@dataclass
class RoutingDecision:
    route: Route
    reason: Optional[str]
    case_id: Optional[str]
    open_flags: list[FlagReason] = field(default_factory=list)
    auto_resolved: list[AutoResolved] = field(default_factory=list)
    out_of_scope: list[FlagReason] = field(default_factory=list)
    edge_cases: list[str] = field(default_factory=list)
    records_tasks: list[str] = field(default_factory=list)
    evidence_tasks: list[str] = field(default_factory=list)
    missing_inputs: list[dict[str, str]] = field(default_factory=list)  # {flag, document_type, check_id, reason}
    normalized: dict[str, Any] = field(default_factory=dict)

    @property
    def workers(self) -> list[str]:
        out = []
        if self.records_tasks:
            out.append("records")
        if self.evidence_tasks:
            out.append("evidence")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "reason": self.reason,
            "case_id": self.case_id,
            "open_flags": [f.value for f in self.open_flags],
            "auto_resolved": [{"flag": a.flag.value, "reason": a.reason} for a in self.auto_resolved],
            "out_of_scope": [f.value for f in self.out_of_scope],
            "edge_cases": self.edge_cases,
            "records_tasks": self.records_tasks,
            "evidence_tasks": self.evidence_tasks,
            "missing_inputs": self.missing_inputs,
            "normalized": self.normalized,
        }


# ---------- Helpers ----------

def _merge(lists: list[tuple[str, ...]]) -> list[str]:
    """Combine task lists in order and drop duplicates."""
    seen: dict[str, None] = {}
    for tasks in lists:
        for t in tasks:
            seen.setdefault(t, None)
    return list(seen)


def _human(reason: str, case_id: Optional[str]) -> RoutingDecision:
    return RoutingDecision(route="human_queue", reason=reason, case_id=case_id)


def _valid_case_id(raw: Any) -> Optional[str]:
    if isinstance(raw, dict):
        cid = raw.get("case_id")
        if isinstance(cid, str) and re.match(CASE_ID, cid):
            return cid
    return None


def normalize_case(case: CaseInput) -> dict[str, Any]:
    app = case.application
    is_individual = app.entity_type == "SOLE_PROPRIETORSHIP" or app.tin_type == "SSN"
    control_name = app.owners[0].name if is_individual else app.legal_name
    out: dict[str, Any] = {
        "applied_legal_name": normalize_name(app.legal_name).normalized,
        "name_control": irs_name_control(control_name, is_individual=is_individual),
        "is_individual": is_individual,
    }
    if case.checks.sos_name.matched_name:
        out["sos_matched_name"] = normalize_name(case.checks.sos_name.matched_name).normalized
    if app.dba:
        out["dba"] = normalize_name(app.dba).normalized
    return out


def _check_inputs(case: CaseInput, flags: list[FlagReason]) -> list[dict[str, str]]:
    """Inputs a flag can't be worked without. Missing ones send the case to needs_info."""
    app = case.application
    doc_types = {d.type for d in case.documents}
    missing: list[dict[str, str]] = []
    ec2 = [f for f in flags if ROUTING_TABLE[f].edge_case == "EC-2"]
    if ec2 and app.tin_last4 is None and not (doc_types & {"irs_cp575", "irs_147c"}):
        missing.append({
            "flag": ec2[0].value,
            "document_type": "irs_147c",
            "check_id": "EC2.ein_letter_matches",
            "reason": "EC-2 is flagged but there is neither a submitted TIN nor an IRS EIN letter",
        })
    if FlagReason.TIN_NOT_ISSUED in flags and app.tin_last4 is None and not (doc_types & {"irs_cp575", "irs_147c"}):
        if not missing:
            missing.append({
                "flag": FlagReason.TIN_NOT_ISSUED.value,
                "document_type": "irs_cp575",
                "check_id": "EC4.ein_age",
                "reason": "TIN_NOT_ISSUED is flagged but there is no TIN and no CP 575 letter",
            })
    if FlagReason.DBA_UNVERIFIED in flags and not app.dba:
        if not any(m["flag"] == FlagReason.DBA_UNVERIFIED.value for m in missing):
            missing.append({
                "flag": FlagReason.DBA_UNVERIFIED.value,
                "document_type": "dba_certificate",
                "check_id": "EC1.dba_owner",
                "reason": "DBA_UNVERIFIED is flagged but the application names no DBA",
            })
    return missing


# ---------- Entry point ----------

def route_case(raw: Any) -> tuple[RoutingDecision, Optional[CaseInput]]:
    """Validate the payload and decide the route. Never raises on bad input."""
    case_id = _valid_case_id(raw)
    if not isinstance(raw, dict):
        return _human("malformed payload: not a JSON object", None), None

    raw_flags = raw.get("flag_reasons")
    if not raw_flags:
        return _human("no flags on the case", case_id), None
    if not isinstance(raw_flags, list):
        return _human("malformed payload: flag_reasons is not a list", case_id), None
    known = {f.value for f in FlagReason}
    # Flags may be any JSON value here (a dict or list isn't hashable), so test strings only.
    unknown = sorted({str(f) for f in raw_flags if not (isinstance(f, str) and f in known)})
    if unknown:
        return _human(f"unknown flags: {unknown}", case_id), None

    try:
        case = CaseInput.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        return _human(f"malformed payload: {loc}: {first.get('msg')}", case_id), None

    flags = list(dict.fromkeys(case.flag_reasons))
    out_of_scope = [f for f in flags if f in OUT_OF_SCOPE]
    in_scope = [f for f in flags if f not in OUT_OF_SCOPE]
    if not in_scope:
        decision = _human("no in-scope flags (EC-1/2/4)", case.case_id)
        decision.out_of_scope = out_of_scope
        return decision, case

    normalized = normalize_case(case)
    auto: list[AutoResolved] = []
    open_flags: list[FlagReason] = []
    matched = case.checks.sos_name.matched_name
    for f in in_scope:
        if f == FlagReason.NAME_SIMILAR_MATCH and matched:
            cmp = compare_names(case.application.legal_name, matched)
            if cmp.at_least("normalized_equal"):
                auto.append(AutoResolved(f, f"Same name after normalization: "
                                            f"'{case.application.legal_name}' vs '{matched}'"))
                continue
        open_flags.append(f)

    decision = RoutingDecision(
        route="reviewed",
        reason=None,
        case_id=case.case_id,
        open_flags=open_flags,
        auto_resolved=auto,
        out_of_scope=out_of_scope,
        normalized=normalized,
    )

    missing = _check_inputs(case, open_flags)
    if missing:
        decision.route = "needs_info"
        decision.reason = "; ".join(m["reason"] for m in missing)
        decision.missing_inputs = missing
        return decision, case

    routes = [ROUTING_TABLE[f] for f in open_flags]
    decision.edge_cases = list(dict.fromkeys(r.edge_case for r in routes))
    decision.records_tasks = _merge([r.records for r in routes])
    decision.evidence_tasks = _merge([r.evidence for r in routes])
    return decision, case


# ---------- Idempotency ----------

def idempotency_key(raw: Any) -> str:
    """case_id + a hash of the canonical input.

    A retry of the same payload gets the same key (no second run); a case that comes
    back after docs_requested with new evidence gets a new one.
    """
    case_id = _valid_case_id(raw) or "case_unknown"
    payload = raw if isinstance(raw, dict) else {"raw": raw}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{case_id}:{digest}"


class IdempotencyStore:
    """Maps an idempotency key to the review it started. In memory; swap for a DB table."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: dict[str, str] = {}

    def get_or_claim(self, key: str, review_id: str) -> tuple[str, bool]:
        """Return (review_id, created). If the key was seen, return the earlier review."""
        with self._lock:
            existing = self._keys.get(key)
            if existing is not None:
                return existing, False
            self._keys[key] = review_id
            return review_id, True

    def release(self, key: str) -> None:
        with self._lock:
            self._keys.pop(key, None)


__all__ = [
    "EVIDENCE_TASKS",
    "OUT_OF_SCOPE",
    "RECORDS_TASKS",
    "ROUTING_TABLE",
    "AutoResolved",
    "FlagRoute",
    "IdempotencyStore",
    "RoutingDecision",
    "idempotency_key",
    "normalize_case",
    "route_case",
]
