"""Step 7: targeted follow-ups and the stopping rules for the loop. Plain code.

Every follow-up addresses one check, asks one side, and requests one specific thing.
Internal follow-ups (asking a worker again, with different search terms) come first:
they cost nothing and don't bother the merchant. Only what is still open afterwards
becomes a merchant request, and there is a single merchant round.

The loop:
1. The Lead creates follow-ups (check_id, target, ask, reason), at most one per open
   required check per round.
2. Only the affected worker runs, and only for that check. The new findings are merged
   in and only that check is re-reconciled.
3. It stops when nothing is open, a HARD_FAIL appears, ``MAX_INTERNAL_ROUNDS`` rounds
   are used, the last round found nothing new, or no internal step is left.

The LLM may reword an ``ask`` to be more specific; it never decides whether to send one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional

from app.graph.facts import EIN_LETTERS, FactIndex, text
from app.graph.plan import Plan, case_context
from app.graph.reconcile import decide, open_required
from kyb_contracts.case_input import TIN_PATTERN, CaseInput
from kyb_contracts.memo import CheckOutcome, DocumentRequest, FollowUpRecord
from kyb_contracts.worker_io import Finding, WorkerInput, WorkerResult

MAX_INTERNAL_ROUNDS = 3
FOLLOW_UP_TOOL_CALLS = 4
R_ID_STRIDE = 20   # parallel Records follow-ups each get their own R-number range (max_tool_calls <= 20)


@dataclass(frozen=True)
class InternalStep:
    target: Literal["records", "evidence"]
    ask: str
    focus: tuple[str, ...]


@dataclass(frozen=True)
class MerchantAsk:
    documents: tuple[str, ...]   # RequestableDocument values, first one not already in the case wins
    ask: str


_REGISTRY_RETRY = InternalStep(
    "records",
    "Search the {state} registry by the DBA '{dba}' and by entity number {entity_id}, "
    "and check for registrations of '{legal_name}' in other states.",
    ("legal_name", "entity_status", "state_entity_id", "former_name"),
)
_FILING_HISTORY = InternalStep(
    "records",
    "Read the filing history of entity {entity_id}: is '{other_name}' a former name of it, "
    "or a separate company?",
    ("former_name", "name_change", "separate_entity"),
)
_COUNTY_DBA = InternalStep(
    "records",
    "Search county-level DBA records in {state} for '{dba}' and report the registered owner.",
    ("dba_name", "dba_owner"),
)
_TIN_RETRY = InternalStep(
    "records",
    "Run the TIN match again with the name '{retry_name}' and report the result and the name control.",
    ("tin_match", "irs_name_control"),
)
_PENDING = InternalStep(
    "records",
    "Check the filing history and pending filings in {state} for '{legal_name}'.",
    ("registry_found", "pending_filing", "formation_date"),
)
_KYC = InternalStep(
    "records",
    "Read the owner KYC result for {owner_ref} and run the TIN match with the owner's name '{owner}'.",
    ("kyc_result", "tin_match"),
)
_EVIDENCE_EIN = InternalStep(
    "evidence",
    "Re-read the IRS letters (CP 575 / 147C) and the W-9: report the name, EIN last 4, name control and date.",
    ("name", "ein_last4", "name_control", "date", "tin_type"),
)
_EVIDENCE_OPERATING = InternalStep(
    "evidence",
    "Report the bank verification status, any business license status, and the website's domain age.",
    ("bank_status", "license_status", "domain_age_days"),
)
_EVIDENCE_NAMES = InternalStep(
    "evidence",
    "Report the exact legal name and DBA each document, the bank account and the website show.",
    ("legal_name", "dba", "registrant", "account_holder"),
)

PLAYBOOK: dict[str, tuple[tuple[InternalStep, ...], MerchantAsk]] = {
    "EC1.entity_active": ((_REGISTRY_RETRY, _FILING_HISTORY),
                          MerchantAsk(("good_standing", "articles"), "A current certificate of good standing")),
    "EC1.name_normalized": ((_REGISTRY_RETRY, _FILING_HISTORY),
                            MerchantAsk(("articles_of_amendment", "good_standing"),
                                        "Articles of amendment or a good standing certificate showing the current legal name")),
    "EC1.bank_account_holder": ((_REGISTRY_RETRY, _EVIDENCE_NAMES),
                                MerchantAsk(("bank_letter", "dba_certificate"),
                                            "A bank letter showing the account holder, or the DBA certificate")),
    "EC1.dba_owner": ((_COUNTY_DBA, _FILING_HISTORY),
                      MerchantAsk(("dba_certificate",), "The DBA filing showing the owner")),
    "EC2.tin_match": ((_TIN_RETRY, _FILING_HISTORY),
                      MerchantAsk(("irs_147c",), "IRS 147C (EIN verification letter)")),
    "EC2.name_control": ((_TIN_RETRY,),
                         MerchantAsk(("irs_147c",), "IRS 147C showing the name the IRS has on file")),
    "EC2.ein_letter_matches": ((_EVIDENCE_EIN,),
                               MerchantAsk(("irs_147c", "irs_cp575"), "IRS 147C or CP 575 for the submitted EIN")),
    "EC2.tin_type_fits": ((), MerchantAsk(("w9",), "A signed W-9 showing the TIN type")),
    "EC4.registry_presence": ((_PENDING,),
                              MerchantAsk(("articles",), "Stamped articles or certificate of formation")),
    "EC4.ein_age": ((_TIN_RETRY,), MerchantAsk(("irs_147c", "irs_cp575"), "IRS 147C (EIN verification letter)")),
    "EC4.sole_prop_owner": ((_KYC,), MerchantAsk(("w9",), "A W-9 in the owner's name with the owner's TIN")),
    "EC4.sole_prop_dba": ((_COUNTY_DBA,), MerchantAsk(("dba_certificate",), "The DBA filing showing the owner")),
    "EC4.operating": ((_EVIDENCE_OPERATING,),
                      MerchantAsk(("business_license", "bank_letter"), "A current business license")),
}
_GENERIC = ((_EVIDENCE_NAMES,), MerchantAsk(("bank_letter",), "A bank letter showing the account holder"))


@dataclass
class PendingFollowUp:
    follow_up_id: str
    check_id: str
    target: Literal["records", "evidence"]
    round: int
    ask: str
    reason: str
    task_id: str
    status_before: str
    sent_at: datetime
    focus: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["sent_at"] = self.sent_at.isoformat()
        d["focus"] = list(self.focus)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PendingFollowUp":
        d = dict(d)
        d["sent_at"] = datetime.fromisoformat(d["sent_at"])
        d["focus"] = tuple(d.get("focus", ()))
        return cls(**d)


# ---------- Stopping rules ----------

def stop_reason(outcomes: dict[str, CheckOutcome], internal_rounds: int,
                last_round_new_findings: Optional[bool], has_candidates: bool) -> Optional[str]:
    """Why the follow-up loop stops now, or None to run another internal round."""
    verdict = decide(outcomes)
    if verdict == "hard_fail":
        return "hard_fail"
    if verdict == "clear":
        return "resolved"
    if internal_rounds >= MAX_INTERNAL_ROUNDS:
        return "max_rounds"
    if last_round_new_findings is False:
        return "no_new_findings"
    if not has_candidates:
        return "no_internal_follow_up_left"
    return None


# ---------- Building follow-ups ----------

def _fill(template: str, case: CaseInput, facts: FactIndex, outcome: CheckOutcome) -> str:
    app = case.application
    letter_names = [text(f.value) for f in facts.evidence("name", kinds=EIN_LETTERS) if text(f.value)]
    other = next((n for n in letter_names if n.upper() != app.legal_name.upper()), None)
    owner = app.owners[0]
    individual = app.entity_type == "SOLE_PROPRIETORSHIP" or app.tin_type == "SSN"
    retry_name = owner.name if individual else (other or app.legal_name)
    values = {
        "state": app.state,
        "dba": app.dba or app.legal_name,
        "entity_id": app.state_entity_id or "(none given)",
        "legal_name": app.legal_name,
        "other_name": other or str(outcome.records_value or outcome.evidence_value or app.legal_name),
        "retry_name": retry_name,
        "owner": owner.name,
        "owner_ref": owner.ref,
    }
    return template.format(**values)


def _tried(history: list[dict[str, Any]], check_id: str) -> int:
    return sum(1 for h in history if h["check_id"] == check_id and h["target"] in ("records", "evidence"))


def next_follow_ups(case: CaseInput, outcomes: dict[str, CheckOutcome], facts: FactIndex,
                    history: list[dict[str, Any]], round_no: int, now: datetime) -> list[PendingFollowUp]:
    """One internal follow-up per open required check, using the next untried step."""
    out: list[PendingFollowUp] = []
    n = len(history)
    for o in open_required(outcomes):
        steps, _ = PLAYBOOK.get(o.check_id, _GENERIC)
        k = _tried(history, o.check_id)
        if k >= len(steps):
            continue
        step = steps[k]
        n += 1
        fid = f"F{n}"
        out.append(PendingFollowUp(
            follow_up_id=fid,
            check_id=o.check_id,
            target=step.target,
            round=round_no,
            ask=_fill(step.ask, case, facts, o),
            reason=f"{o.check_id} is {o.status}" + (f": records {o.records_value!r}" if o.records_value else "")
                   + (f", evidence {o.evidence_value!r}" if o.evidence_value else ""),
            task_id=f"{case.case_id}-{step.target}-{fid}",
            status_before=o.status,
            sent_at=now,
            focus=step.focus,
        ))
    return out


def has_internal_candidates(outcomes: dict[str, CheckOutcome], history: list[dict[str, Any]]) -> bool:
    return any(_tried(history, o.check_id) < len(PLAYBOOK.get(o.check_id, _GENERIC)[0])
               for o in open_required(outcomes))


def safe_ask(candidate: Optional[str], fallback: str) -> str:
    """Accept an LLM-reworded ask only if it is short, specific and has no full TIN."""
    if not candidate:
        return fallback
    c = candidate.strip()
    if not c or len(c) > 400 or TIN_PATTERN.search(c):
        return fallback
    return c


def follow_up_task(case: CaseInput, plan: Plan, fu: PendingFollowUp, results: list[WorkerResult],
                   facts: FactIndex, initial_task_ids: dict[str, str], slot: int) -> WorkerInput:
    """A FollowUp for one worker and one check. Prior findings come from the same worker only,
    so records are never shaped by what the merchant said, and vice versa."""
    prior: list[Finding] = []
    for r in results:
        if r.worker == fu.target:
            prior += [f for f in r.findings if f.field in fu.focus]
    base = facts.max_record_number() + 1
    return WorkerInput(
        task_id=fu.task_id,
        worker=fu.target,
        question=f"[{fu.check_id}] {fu.ask}",
        focus_fields=list(fu.focus),
        hypotheses=plan.hypotheses,
        context=case_context(case),
        prior_findings=prior[-10:],
        follow_up_of=initial_task_ids.get(fu.target),
        max_tool_calls=FOLLOW_UP_TOOL_CALLS,
        record_id_start=base + slot * R_ID_STRIDE,
    )


def to_record(fu: PendingFollowUp, status_after: str, new_evidence: list[str],
              answered_at: datetime) -> FollowUpRecord:
    return FollowUpRecord(
        follow_up_id=fu.follow_up_id,
        check_id=fu.check_id,
        target=fu.target,
        round=fu.round,
        ask=fu.ask,
        reason=fu.reason,
        task_id=fu.task_id,
        status_before=fu.status_before,
        status_after=status_after,
        new_evidence=new_evidence,
        sent_at=fu.sent_at,
        answered_at=answered_at,
    )


# ---------- The merchant round ----------

def merchant_requests(case: CaseInput, open_checks: list[CheckOutcome],
                      drop: frozenset[str] = frozenset()) -> list[DocumentRequest]:
    """One document per open check, never one the case already has."""
    have = {d.type for d in case.documents}
    out: list[DocumentRequest] = []
    seen: set[str] = set()
    for o in open_checks:
        _, merchant = PLAYBOOK.get(o.check_id, _GENERIC)
        doc = next((d for d in merchant.documents if d not in have and d not in drop), None)
        if doc is None or doc in seen:
            continue
        seen.add(doc)
        out.append(DocumentRequest(document_type=doc, check_id=o.check_id,
                                   reason=f"{merchant.ask} would settle {o.check_id} ({o.status})"))
    return out


def merchant_record(requests: list[DocumentRequest], outcomes: dict[str, CheckOutcome],
                    follow_up_number: int, now: datetime) -> Optional[FollowUpRecord]:
    if not requests:
        return None
    first = requests[0]
    _, merchant = PLAYBOOK.get(first.check_id, _GENERIC)
    o = outcomes[first.check_id]
    return FollowUpRecord(
        follow_up_id=f"F{follow_up_number}",
        check_id=first.check_id,
        target="merchant",
        round=1,
        ask=merchant.ask,
        reason=f"{first.check_id} is still {o.status} after the internal follow-ups",
        status_before=o.status,
        sent_at=now,
    )


__all__ = [
    "FOLLOW_UP_TOOL_CALLS",
    "MAX_INTERNAL_ROUNDS",
    "PLAYBOOK",
    "InternalStep",
    "MerchantAsk",
    "PendingFollowUp",
    "follow_up_task",
    "has_internal_candidates",
    "merchant_record",
    "merchant_requests",
    "next_follow_ups",
    "safe_ask",
    "stop_reason",
    "to_record",
]
