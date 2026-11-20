"""Step 8: write the memo.

Code fills every structured part: per-check statuses, the recommendation, confidence,
``auto_resolved``, ``out_of_scope``, the follow-up history and the document requests.
The LLM (when configured) writes only the prose: root causes, causes ruled out, and
notes for the analyst, citing evidence IDs. Anything it cites that doesn't exist is
dropped, and anything shaped like a full TIN is redacted (and reported in step 9).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from pydantic import BaseModel, Field

from app.graph.facts import FactIndex
from app.graph.followup import merchant_record, merchant_requests
from app.graph.orchestrator import RoutingDecision
from app.graph.plan import TEMPLATES, Plan
from app.graph.reconcile import decide
from kyb_contracts.case_input import TIN_PATTERN, CaseInput, FlagReason
from kyb_contracts.memo import (
    PASSING_STATUSES,
    CheckOutcome,
    DocumentRequest,
    FlagOutcome,
    FollowUpRecord,
    Memo,
    RuledOut,
    weakest,
)

RECHECK_AFTER_DAYS = 30
_EVIDENCE_ID = re.compile(r"^[RDBWK]\d+$")


# ---------- What the LLM may write ----------

class FlagProse(BaseModel):
    flag: str
    root_cause: str


class RuledOutProse(BaseModel):
    cause: str
    evidence: list[str] = Field(default_factory=list)


class MemoProse(BaseModel):
    """Prose only. Statuses, the recommendation and confidence are never the LLM's call."""

    flags: list[FlagProse] = Field(default_factory=list)
    ruled_out: list[RuledOutProse] = Field(default_factory=list)
    notes_for_analyst: str = ""


@dataclass
class Revision:
    """Fixes step 9 asks for when it sends the memo back once."""

    unresolve_flags: set[str] = field(default_factory=set)
    force_escalate: bool = False
    drop_doc_types: set[str] = field(default_factory=set)
    failures: list[str] = field(default_factory=list)


@dataclass
class MemoBuild:
    memo: Memo
    tin_redacted: bool = False


def scrub(text: str) -> tuple[str, bool]:
    if TIN_PATTERN.search(text):
        return TIN_PATTERN.sub("[redacted]", text), True
    return text, False


# ---------- Deterministic prose (used when there is no LLM, or as the fallback) ----------

def default_root_cause(flag: str, checks: list[CheckOutcome], resolved: bool) -> str:
    if not checks:
        return "No verification check applied to this flag"
    if resolved:
        explained = [c for c in checks if c.status == "EXPLAINED" and c.explanation]
        if explained:
            return "; ".join(f"{c.description}: {c.explanation}" for c in explained)
        return "Records and merchant evidence agree on: " + "; ".join(c.description for c in checks)
    open_ = [c for c in checks if c.status not in PASSING_STATUSES]
    return "Still open: " + "; ".join(f"{c.description} ({c.status})" for c in open_)


def default_ruled_out(outcomes: dict[str, CheckOutcome]) -> list[RuledOut]:
    out: list[RuledOut] = []
    tin = outcomes.get("EC2.tin_match")
    if tin and tin.status in PASSING_STATUSES:
        ev = tin.explanation_evidence or [e for e in tin.evidence if e.startswith("R")]
        if ev:
            out.append(RuledOut(cause="The EIN belongs to an unrelated company", evidence=ev))
    return out


def default_notes(case: CaseInput, routing: RoutingDecision, outcomes: dict[str, CheckOutcome],
                  recommendation: str, provisional: bool, suspicious: list[str]) -> str:
    parts: list[str] = []
    if routing.route != "reviewed":
        parts.append(f"Not reviewed by the agent: {routing.reason}.")
    hard = [o for o in outcomes.values() if o.status == "HARD_FAIL"]
    if hard:
        parts.append("Hard fail: " + ", ".join(f"{o.check_id} ({o.hard_fail_reason})" for o in hard) + ".")
    open_ = [o for o in outcomes.values() if o.required and o.status in ("GAP", "CONFLICT")]
    if open_ and recommendation != "escalate":
        parts.append("Open checks: " + ", ".join(f"{o.check_id} ({o.status})" for o in open_) + ".")
    if provisional:
        parts.append(f"Provisional clear: re-run the registry lookup after {RECHECK_AFTER_DAYS} days.")
    if suspicious:
        parts.append("Instruction-like or suspicious text was found in " + ", ".join(sorted(set(suspicious)))
                     + "; it was treated as data and not followed.")
    if routing.out_of_scope:
        parts.append("The agent did not review " + ", ".join(f.value for f in routing.out_of_scope) + ".")
    return " ".join(parts)


# ---------- Assembly ----------

def _gap_outcomes_for(check_ids: list[str]) -> list[CheckOutcome]:
    by_id = {t.check_id: t for t in TEMPLATES}
    out = []
    for cid in check_ids:
        t = by_id[cid]
        out.append(CheckOutcome(check_id=cid, edge_case=t.edge_case, description=t.description,
                                required=True, status="GAP", confidence="low"))
    return out


def build_memo(*, case_id: str, case: Optional[CaseInput], routing: RoutingDecision,
               plan: Optional[Plan], outcomes: dict[str, CheckOutcome],
               follow_ups: list[FollowUpRecord], facts: Optional[FactIndex],
               prose: Optional[MemoProse], now: datetime, today: date,
               version: int = 1, revision: Optional[Revision] = None,
               worker_errors: Optional[list[str]] = None) -> MemoBuild:
    revision = revision or Revision()
    redacted = False

    def clean(s: str) -> str:
        nonlocal redacted
        s, hit = scrub(s)
        redacted |= hit
        return s

    auto = [a.flag for a in routing.auto_resolved]

    # ----- Intake routes: human queue or needs info -----
    if routing.route == "human_queue":
        memo = Memo(case_id=case_id, generated_at=now, version=version, route="human_queue",
                    route_reason=clean(routing.reason or "routed to a human"),
                    out_of_scope=routing.out_of_scope, recommendation="insufficient_evidence",
                    confidence="low",
                    notes_for_analyst=clean(f"Routed to the human queue: {routing.reason}."))
        return MemoBuild(memo, redacted)

    if routing.route == "needs_info":
        check_ids = list(dict.fromkeys(m["check_id"] for m in routing.missing_inputs))
        checks = _gap_outcomes_for(check_ids)
        requests = [DocumentRequest(document_type=m["document_type"], check_id=m["check_id"], reason=m["reason"])
                    for m in routing.missing_inputs]
        flags = [FlagOutcome(flag=f, status="unresolved",
                             root_cause=clean(next((m["reason"] for m in routing.missing_inputs
                                                    if m["flag"] == f.value),
                                                   "Not reviewed yet: the case is waiting for missing inputs")),
                             check_ids=[m["check_id"] for m in routing.missing_inputs if m["flag"] == f.value])
                 for f in routing.open_flags]
        memo = Memo(case_id=case_id, generated_at=now, version=version, route="needs_info",
                    route_reason=clean(routing.reason or "inputs missing"), flags=flags,
                    auto_resolved=auto, out_of_scope=routing.out_of_scope, checks=checks,
                    recommendation="request_documents", confidence="low", document_request=requests,
                    notes_for_analyst=clean(f"Inputs were missing before review: {routing.reason}."))
        return MemoBuild(memo, redacted)

    # ----- Reviewed -----
    assert case is not None
    assert plan is not None or not routing.open_flags
    checks = list(outcomes.values())
    verdict = decide(outcomes)
    known_ids = facts.known_evidence_ids() if facts else set()

    flag_outcomes: list[FlagOutcome] = []
    prose_by_flag = {p.flag: p.root_cause for p in (prose.flags if prose else [])}
    for flag in routing.open_flags:
        cids = [c for c in plan.checks_for_flag(flag) if c in outcomes]
        fchecks = [outcomes[c] for c in cids]
        resolved = bool(fchecks) and all(c.status in PASSING_STATUSES for c in fchecks if c.required)
        if flag.value in revision.unresolve_flags:
            resolved = False
        evidence: list[str] = []
        for c in fchecks:
            evidence += c.evidence + c.explanation_evidence
        root = prose_by_flag.get(flag.value) or default_root_cause(flag.value, fchecks, resolved)
        flag_outcomes.append(FlagOutcome(
            flag=flag, status="resolved" if resolved else "unresolved", root_cause=clean(root),
            check_ids=cids, evidence=list(dict.fromkeys(evidence)),
        ))

    # Recommendation: code only.
    requests: list[DocumentRequest] = []
    provisional = False
    if verdict == "hard_fail" or revision.force_escalate:
        recommendation = "escalate"
    elif verdict == "clear" and all(f.status == "resolved" for f in flag_outcomes):
        recommendation = "recommend_clear"
        # A new business whose registry entry hasn't appeared yet: clear now, re-check later.
        # (A sole proprietor is never in the registry, so there is nothing to re-check.)
        provisional = case.application.entity_type != "SOLE_PROPRIETORSHIP" and any(
            c.expected_absence and c.status == "EXPLAINED" for c in checks)
    else:
        still_open = [c for c in checks if c.required and c.status not in PASSING_STATUSES]
        for f in flag_outcomes:  # flags step 9 unresolved, though their checks pass
            if f.status == "unresolved":
                still_open += [outcomes[c] for c in f.check_ids if outcomes[c] not in still_open][:1]
        # A worker that failed is our problem, not the merchant's: don't ask them for documents.
        requests = [] if worker_errors else merchant_requests(case, still_open, frozenset(revision.drop_doc_types))
        recommendation = "request_documents" if requests else "insufficient_evidence"

    history = list(follow_ups)
    if recommendation == "request_documents":
        rec = merchant_record(requests, outcomes, len(history) + 1, now)
        if rec:
            history.append(rec)

    required = [c.confidence for c in checks if c.required]
    confidence = weakest(required) if required else ("high" if recommendation == "recommend_clear" else "low")

    ruled_out: list[RuledOut] = []
    if prose and prose.ruled_out:
        for r in prose.ruled_out:
            ev = [e for e in r.evidence if _EVIDENCE_ID.match(e) and e in known_ids]
            if ev and r.cause.strip():
                ruled_out.append(RuledOut(cause=clean(r.cause), evidence=ev))
    else:
        ruled_out = default_ruled_out(outcomes) if recommendation != "escalate" else []

    suspicious = [f.source_id for f in facts.evidence("suspicious")] if facts else []
    notes = (prose.notes_for_analyst if prose and prose.notes_for_analyst else
             default_notes(case, routing, outcomes, recommendation, provisional, suspicious))
    if routing.out_of_scope and not all(f.value in notes for f in routing.out_of_scope):
        notes += " The agent did not review " + ", ".join(f.value for f in routing.out_of_scope) + "."
    if worker_errors:
        notes += " Worker failures (checks left open): " + "; ".join(worker_errors) + "."
    if revision.failures:
        notes += " Step 9 asked for a revision: " + "; ".join(revision.failures) + "."

    memo = Memo(
        case_id=case_id, version=version, generated_at=now, route="reviewed",
        flags=flag_outcomes, auto_resolved=auto, out_of_scope=routing.out_of_scope,
        checks=checks, ruled_out=ruled_out, recommendation=recommendation, confidence=confidence,
        provisional=provisional,
        recheck_after=(today + timedelta(days=RECHECK_AFTER_DAYS)) if provisional else None,
        recheck_targets=["registry"] if provisional else [],
        document_request=requests, follow_ups=history, notes_for_analyst=clean(notes.strip()),
    )
    return MemoBuild(memo, redacted)


__all__ = [
    "FlagProse",
    "MemoBuild",
    "MemoProse",
    "RECHECK_AFTER_DAYS",
    "Revision",
    "RuledOutProse",
    "build_memo",
    "scrub",
]
