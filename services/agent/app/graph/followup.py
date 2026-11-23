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

Which step comes next also reads what the workers said about their last pass
(``worker_notes.py``): if a worker left something unresolved for this check, its next
step goes to that worker first, and the follow-up question tells the worker what is
still unresolved and what it already searched for without finding a record.

The LLM may reword an ``ask`` to be more specific; it never decides whether to send one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping, Optional, Sequence

from app.graph.facts import EIN_LETTERS, FactIndex, text, worker_fields
from app.graph.plan import Plan, case_context
from app.graph.reconcile import decide, open_required
from app.graph.worker_notes import WorkerNote, follow_up_context, unresolved_workers
from kyb_contracts.case_input import TIN_PATTERN, CaseInput, DocumentType
from kyb_contracts.memo import DocumentRequest
from app.review import CheckOutcome, FollowUpRecord
from kyb_contracts.worker_io import Finding, WorkerInput, WorkerResult

MAX_INTERNAL_ROUNDS = 3
FOLLOW_UP_TOOL_CALLS = 4
R_ID_STRIDE = 20   # parallel Records follow-ups each get their own R-number range (max_tool_calls <= 20)


@dataclass(frozen=True)
class InternalStep:
    name: str                                     # recorded on the FollowUpRecord, so a step runs once
    target: Literal["records", "evidence"]
    ask: str
    focus: tuple[str, ...]


@dataclass(frozen=True)
class MerchantAsk:
    documents: tuple[DocumentType, ...]   # the first one not already in the case wins
    ask: str


@dataclass(frozen=True)
class MerchantRequest:
    """One document to ask the merchant for, and the check it would settle.

    The memo carries the ``DocumentRequest`` (type, reason); the check goes in the
    Lead's report (``review.DocumentCheck``)."""

    document_type: DocumentType
    check_id: str
    reason: str

    def to_memo(self) -> DocumentRequest:
        return DocumentRequest(type=self.document_type, reason=self.reason)


_REGISTRY_RETRY = InternalStep(
    "registry_retry", "records",
    "Search the {state} registry by the DBA '{dba}' and by entity number {entity_id}, "
    "and check for registrations of '{legal_name}' in other states.",
    ("legal_name", "entity_status", "state_entity_id", "former_names", "former_name", "registry_match"),
)
_FILING_HISTORY = InternalStep(
    "filing_history", "records",
    "Read the filing history of entity {entity_id}: is '{other_name}' a former name of it, "
    "or a separate company?",
    ("former_name", "former_names", "filing_event", "name_change", "separate_entity"),
)
_APPLIED_NAME = InternalStep(
    "applied_name", "records",
    "Read entity {entity_id}'s registry record and filing history, and search DBA filings for "
    "'{legal_name}': is '{legal_name}' a former name or an assumed name of that entity, and who are its officers?",
    ("legal_name", "state_entity_id", "officer_name", "former_names", "former_name", "filing_event", "dba",
     "registrant", "registrant_state_entity_id"),
)
_COUNTY_DBA = InternalStep(
    "county_dba", "records",
    "Search county-level DBA records in {state} for '{dba}' and report the registered owner.",
    ("dba", "registrant", "registrant_state_entity_id", "dba_name", "dba_owner"),
)
_TIN_RETRY = InternalStep(
    "tin_retry", "records",
    "Run the TIN match again with the name '{retry_name}' and report the result and the name control.",
    ("tin_match_result", "name_control_tested", "former_names", "former_name", "tin_match", "irs_name_control"),
)
_PENDING = InternalStep(
    "pending_filing", "records",
    "Search the {state} registry for '{legal_name}' and read its filing history: is a formation filed "
    "but still pending?",
    ("registry_match", "registry_entity", "filing_history", "formation_date", "entity_status",
     "registry_found"),
)
_KYC = InternalStep(
    "owner_kyc", "records",
    "Read the owner KYC result for {owner_ref} and run the TIN match with the owner's name '{owner}'.",
    ("kyc_result", "tin_match_result", "name_control_tested", "tin_match"),
)
_EVIDENCE_EIN = InternalStep(
    "ein_letters", "evidence",
    "Re-read the IRS letters (CP 575 / 147C) and the W-9: report the name, EIN last 4, name control and date.",
    ("name", "ein_last4", "name_control", "date", "tin_type", "tin_last4", "legal_name", "business_name"),
)
_EVIDENCE_OPERATING = InternalStep(
    "operating", "evidence",
    "Report the bank verification status, any business license status, and the name the website shows.",
    ("bank_status", "account_holder", "status", "holder", "website_legal_name", "license_status"),
)
_EVIDENCE_NAMES = InternalStep(
    "evidence_names", "evidence",
    "Report the exact legal name and DBA each document, the bank account and the website show.",
    ("legal_name", "name", "holder", "dba", "registrant", "account_holder", "website_legal_name",
     "website_dba"),
)

PLAYBOOK: dict[str, tuple[tuple[InternalStep, ...], MerchantAsk]] = {
    "EC1.entity_active": ((_REGISTRY_RETRY, _FILING_HISTORY),
                          MerchantAsk(("good_standing", "articles"), "A current certificate of good standing")),
    "EC1.name_normalized": ((_REGISTRY_RETRY, _FILING_HISTORY),
                            MerchantAsk(("articles_of_amendment", "good_standing"),
                                        "Articles of amendment or a good standing certificate showing the current legal name")),
    "EC1.bank_account_holder": ((_REGISTRY_RETRY, _EVIDENCE_NAMES),
                                MerchantAsk(("dba_certificate", "articles_of_amendment"),
                                            "The DBA certificate or articles of amendment that tie the bank account "
                                            "holder's name to the legal entity")),
    "EC1.applied_name": ((_APPLIED_NAME,),
                         MerchantAsk(("dba_certificate", "articles_of_amendment"),
                                     "A DBA certificate or articles of amendment that tie the applied name to "
                                     "the registered entity")),
    "EC1.dba_owner": ((_COUNTY_DBA, _FILING_HISTORY),
                      MerchantAsk(("dba_certificate",), "A current DBA certificate filed by the legal entity")),
    "EC2.tin_match": ((_TIN_RETRY, _FILING_HISTORY),
                      MerchantAsk(("irs_147c",), "IRS 147C (EIN verification letter)")),
    "EC2.name_control": ((_TIN_RETRY,),
                         MerchantAsk(("irs_147c",), "IRS 147C showing the name the IRS has on file")),
    "EC2.ein_letter_matches": ((_EVIDENCE_EIN,),
                               MerchantAsk(("irs_147c", "irs_cp575"), "IRS 147C or CP 575 for the submitted EIN")),
    "EC2.tin_type_fits": ((), MerchantAsk(("w9",), "A signed W-9 showing the TIN type")),
    "EC4.registry_presence": ((_PENDING,),
                              MerchantAsk(("articles", "good_standing"),
                                          "Stamped articles of formation, or a certificate of good standing "
                                          "once the filing is approved")),
    "EC4.ein_age": ((_TIN_RETRY,), MerchantAsk(("irs_147c", "irs_cp575"), "IRS 147C (EIN verification letter)")),
    "EC4.sole_prop_owner": ((_KYC,), MerchantAsk(("w9",), "A W-9 in the owner's name with the owner's TIN")),
    "EC4.sole_prop_dba": ((_COUNTY_DBA,), MerchantAsk(("dba_certificate",), "The DBA filing showing the owner")),
    "EC4.operating": ((_EVIDENCE_OPERATING,),
                      MerchantAsk(("business_license",), "A current business license")),
}
# Lead-added checks are optional, so they never reach the merchant round.
_GENERIC = ((_EVIDENCE_NAMES,), MerchantAsk((), "No merchant document for a lead-added check"))


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
    step: str = ""
    context: str = ""            # what the worker left open last time; added to the question

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


def _untried(history: list[dict[str, Any]], check_id: str) -> list[InternalStep]:
    """The check's playbook steps not run yet, in playbook order."""
    steps = list(PLAYBOOK.get(check_id, _GENERIC)[0])
    for h in history:
        if h["check_id"] != check_id or h["target"] not in ("records", "evidence"):
            continue
        done = next((s for s in steps if s.name == h.get("step")), None)
        if done is None and not h.get("step"):   # a record from before steps were named: oldest first
            done = steps[0] if steps else None
        if done is not None:
            steps.remove(done)
    return steps


def choose_step(untried: list[InternalStep], notes: Sequence[WorkerNote] = ()) -> Optional[InternalStep]:
    """The next step: the first one aimed at a worker that left this check unresolved, else
    the next in playbook order. (A worker's not_found doesn't skip its steps: a different
    search, e.g. county DBA records after the state's, is often exactly what is needed.)"""
    if not untried:
        return None
    unsettled = unresolved_workers(notes)
    return next((s for s in untried if s.target in unsettled), untried[0])


def next_follow_ups(case: CaseInput, outcomes: dict[str, CheckOutcome], facts: FactIndex,
                    history: list[dict[str, Any]], round_no: int, now: datetime,
                    notes: Optional[Mapping[str, Sequence[WorkerNote]]] = None) -> list[PendingFollowUp]:
    """One internal follow-up per open required check, using the best untried step.

    ``notes``: per check, what the workers reported as unresolved or not found
    (``worker_notes.notes_by_check``)."""
    notes = notes or {}
    out: list[PendingFollowUp] = []
    n = len(history)
    for o in open_required(outcomes):
        check_notes = list(notes.get(o.check_id, ()))
        step = choose_step(_untried(history, o.check_id), check_notes)
        if step is None:
            continue
        n += 1
        fid = f"F{n}"
        reported = [x.render() for x in check_notes if x.worker == step.target]
        out.append(PendingFollowUp(
            follow_up_id=fid,
            check_id=o.check_id,
            target=step.target,
            round=round_no,
            ask=_fill(step.ask, case, facts, o),
            reason=f"{o.check_id} is {o.status}" + (f": records {o.records_value!r}" if o.records_value else "")
                   + (f", evidence {o.evidence_value!r}" if o.evidence_value else "")
                   + (f"; {'; '.join(reported)}" if reported else ""),
            task_id=f"{case.case_id}-{step.target}-{fid}",
            status_before=o.status,
            sent_at=now,
            focus=step.focus,
            step=step.name,
            context=follow_up_context(check_notes, step.target),
        ))
    return out


def has_internal_candidates(outcomes: dict[str, CheckOutcome], history: list[dict[str, Any]]) -> bool:
    return any(_untried(history, o.check_id) for o in open_required(outcomes))


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
    focus = worker_fields(fu.target, fu.focus)
    prior: list[Finding] = []
    for r in results:
        if r.worker == fu.target:
            prior += [f for f in r.findings if f.field in focus]
    base = facts.max_record_number() + 1
    return WorkerInput(
        task_id=fu.task_id,
        worker=fu.target,
        question=f"[{fu.check_id}] {fu.ask}" + (f" {fu.context}" if fu.context else ""),
        focus_fields=focus,
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
        step=fu.step or None,
        status_before=fu.status_before,
        status_after=status_after,
        new_evidence=new_evidence,
        sent_at=fu.sent_at,
        answered_at=answered_at,
    )


# ---------- The merchant round ----------

def merchant_requests(case: CaseInput, open_checks: list[CheckOutcome],
                      drop: frozenset[str] = frozenset()) -> list[MerchantRequest]:
    """One document per open check, never one the case already has."""
    have = {d.type for d in case.documents}
    out: list[MerchantRequest] = []
    seen: set[str] = set()
    for o in open_checks:
        _, merchant = PLAYBOOK.get(o.check_id, _GENERIC)
        doc = next((d for d in merchant.documents if d not in have and d not in drop), None)
        if doc is None or doc in seen:
            continue
        seen.add(doc)
        out.append(MerchantRequest(document_type=doc, check_id=o.check_id,
                                   reason=f"{merchant.ask} would settle {o.check_id} ({o.status})"))
    return out


def merchant_record(requests: list[MerchantRequest], outcomes: dict[str, CheckOutcome],
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
    "MerchantRequest",
    "PendingFollowUp",
    "choose_step",
    "follow_up_task",
    "has_internal_candidates",
    "merchant_record",
    "merchant_requests",
    "next_follow_ups",
    "safe_ask",
    "stop_reason",
    "to_record",
]
