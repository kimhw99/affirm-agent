"""The Lead's report beside the memo, and what ``GET /reviews/{review_id}`` returns.

Owned by the agent service, not ``/contracts``: the shared contracts (memo.py,
worker_io.py, ...) are used exactly as merged on main, and this module only wraps them.

memo.py is the agreed memo (BEAC-22): per-flag outcomes, the causes ruled out, one
recommendation, the documents to request, notes, plus ``CheckResult``,
``evidence_index`` and ``CheckedMemo``. This module adds what the Lead produces on top
of it and doesn't change memo.py:

- ``LeadReport``: the Lead's working detail, filled by code. The per-check reconcile
  statuses (``CheckOutcome``), the overall confidence (the weakest required check),
  the intake route, a provisional clear, the follow-up history, and which checks back
  each flag and each document request.
- ``ReviewOutput``: the ``CheckedMemo`` (memo + evidence_index + check_result, exactly
  as memo.py defines them) plus the ``LeadReport``, the Lead's own guardrail failures,
  and excerpts for evidence the report cites but the memo doesn't.

Producer: the agent service (H). Consumers: the review app (XL), evaluation.

Two names that are easy to mix up:

- ``CheckOutcome`` is one *verification check* from the Lead's plan (for example "the
  DBA belongs to the legal entity") and its reconcile status.
- ``CheckResult`` (memo.py) is the step 9 *guardrail* result for the whole memo.

How the brief's outcomes map onto the memo's ``Recommendation``:

| Outcome                                   | memo.recommendation    | LeadReport                          |
|-------------------------------------------|------------------------|-------------------------------------|
| clear                                     | recommend_clear        |                                     |
| provisional clear (EC-4 expected absence) | recommend_clear        | provisional, recheck_after          |
| any HARD_FAIL (escalate or reject)        | escalate               | hard_fail_reason on the check       |
| conflict needs a merchant document        | request_documents      | document_checks                     |
| intake: needs_info (inputs missing)       | request_documents      | route="needs_info"                  |
| intake: out-of-scope flags only           | insufficient_evidence  | route="human_queue"                 |
| intake: no flags or a malformed payload   | (no memo)              | route="human_queue", route_reason   |
| conflicts left open after follow-ups      | insufficient_evidence  | the open checks in ``checks``       |

A memo needs at least one flag (memo.py), so a payload with no flags or one that
doesn't parse gets no memo at all: ``checked_memo`` is ``None`` and the analyst sees
the route reason.

Rules enforced here: structure and consistency between the report and the memo, plus
one safety rule that must be impossible to break: a HARD_FAIL check means the memo
recommends escalate.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import ConfigDict, Field, model_validator

from kyb_contracts.case_input import CASE_ID, Contract, DocumentType, FlagReason
from kyb_contracts.memo import CheckedMemo, EvidenceEntry, Recommendation
from kyb_contracts.worker_io import EVIDENCE_ID, Confidence

# ---------- Fixed values ----------

EdgeCase = Literal["EC-1", "EC-2", "EC-4"]

# Which edge case each in-scope flag belongs to. A flag outside this table is out of scope.
FLAG_EDGE_CASE: dict[FlagReason, EdgeCase] = {
    FlagReason.NAME_SIMILAR_MATCH: "EC-1",
    FlagReason.NAME_UNVERIFIED: "EC-1",
    FlagReason.DBA_UNVERIFIED: "EC-1",
    FlagReason.TIN_MISMATCH: "EC-2",
    FlagReason.TIN_NOT_FOUND: "EC-2",
    FlagReason.TIN_NOT_ISSUED: "EC-4",
    FlagReason.ENTITY_NOT_FOUND: "EC-4",
    FlagReason.NO_COMMERCIAL_RECORD: "EC-4",
}

# Template checks use their edge case's prefix (EC1.); checks the Lead adds use ADD.
CHECK_ID = r"^(EC1|EC2|EC4|ADD)\.[a-z0-9_]+$"
FOLLOW_UP_ID = r"^F\d+$"

_EDGE_CASE_PREFIX: dict[str, str] = {"EC-1": "EC1.", "EC-2": "EC2.", "EC-4": "EC4."}
_EVIDENCE_ID_RE = re.compile(EVIDENCE_ID)


class CheckStatus(str, Enum):
    """Reconcile status of one check (strings, so they serialize as-is)."""

    AGREE = "AGREE"            # both sides found the same thing (after normalization)
    EXPLAINED = "EXPLAINED"    # they differ, but a cited, documented reason connects them
    GAP = "GAP"                # one side has no finding
    CONFLICT = "CONFLICT"      # both sides found something and it doesn't match
    HARD_FAIL = "HARD_FAIL"    # blocks approval on its own


CheckStatusValue = Literal["AGREE", "EXPLAINED", "GAP", "CONFLICT", "HARD_FAIL"]
PASSING_STATUSES: frozenset[str] = frozenset({"AGREE", "EXPLAINED"})

HardFailReason = Literal[
    "tin_never_issued",
    "entity_dissolved_or_revoked",
    "kyc_failed",
    "ein_letter_mismatch",
    "ein_belongs_to_other_entity",
    "document_tampered",        # reserved: no worker reports tampering yet, so nothing sets it today
    "other",
]

Route = Literal["reviewed", "needs_info", "human_queue"]

# The Lead's guardrails beyond memo.CheckRule. They send the memo back once, like the
# memo's own rules, and are shown to the analyst as warnings when they still fail.
LeadRule = Literal[
    "HARD_FAIL_NOT_ESCALATED",      # a blocking finding exists but the memo doesn't escalate
    "REQUIRED_CHECK_MISSING",       # a required template check is missing from the report
    "DOCUMENT_ALREADY_IN_CASE",     # never ask the merchant for a document type the case has
    "WEBSITE_OVERRULES_RECORDS",    # an explanation can't rest on the merchant's website alone
]

_CONFIDENCE_RANK: dict[str, int] = {
    Confidence.LOW.value: 0,
    Confidence.MEDIUM.value: 1,
    Confidence.HIGH.value: 2,
}


def weakest(levels: list[str]) -> str:
    """Overall confidence is the weakest check's, never an average."""
    return min(levels, key=_CONFIDENCE_RANK.__getitem__)


def _bad_ids(values: list[str]) -> list[str]:
    return [v for v in values if not _EVIDENCE_ID_RE.match(v)]


class _Model(Contract):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


# ---------- Report parts ----------

class CheckOutcome(_Model):
    """One claim from the Lead's plan and how reconcile settled it."""

    check_id: str = Field(pattern=CHECK_ID)
    edge_case: EdgeCase
    description: str
    origin: Literal["template", "lead_added"] = "template"
    required: bool = True
    expected_absence: bool = False      # EC-4: "not in the registry" was expected up front
    records_value: Optional[Any] = None
    evidence_value: Optional[Any] = None
    status: CheckStatusValue
    confidence: Confidence
    evidence: list[str] = Field(default_factory=list)
    explanation: Optional[str] = None
    explanation_evidence: list[str] = Field(default_factory=list)
    hard_fail_reason: Optional[HardFailReason] = None

    @model_validator(mode="after")
    def _status_needs_support(self) -> "CheckOutcome":
        bad = _bad_ids(self.evidence + self.explanation_evidence)
        if bad:
            raise ValueError(f"check {self.check_id}: not evidence IDs: {bad}")
        if self.status == "EXPLAINED" and not (self.explanation and self.explanation_evidence):
            raise ValueError(
                f"check {self.check_id}: EXPLAINED needs an explanation that cites evidence; "
                "without one it is a CONFLICT"
            )
        if (self.status == "HARD_FAIL") != (self.hard_fail_reason is not None):
            raise ValueError(
                f"check {self.check_id}: hard_fail_reason is set if and only if the status is HARD_FAIL"
            )
        if self.origin == "lead_added" and not self.check_id.startswith("ADD."):
            raise ValueError(f"check {self.check_id}: lead-added checks use the ADD. prefix")
        if self.origin == "template" and not self.check_id.startswith(_EDGE_CASE_PREFIX[self.edge_case]):
            raise ValueError(f"check {self.check_id}: template checks use their edge case's prefix")
        return self


class FollowUpRecord(_Model):
    """One targeted follow-up and what came of it."""

    follow_up_id: str = Field(pattern=FOLLOW_UP_ID)
    check_id: str = Field(pattern=CHECK_ID)
    target: Literal["records", "evidence", "merchant"]
    round: int = Field(ge=1)
    ask: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    task_id: Optional[str] = None
    step: Optional[str] = None          # the playbook step (followup.PLAYBOOK) an internal follow-up ran
    status_before: CheckStatusValue
    status_after: Optional[CheckStatusValue] = None
    new_evidence: list[str] = Field(default_factory=list)
    sent_at: datetime
    answered_at: Optional[datetime] = None

    @model_validator(mode="after")
    def _shape(self) -> "FollowUpRecord":
        bad = _bad_ids(self.new_evidence)
        if bad:
            raise ValueError(f"follow-up {self.follow_up_id}: not evidence IDs: {bad}")
        if self.target in ("records", "evidence"):
            if not self.task_id:
                raise ValueError(f"follow-up {self.follow_up_id}: an internal follow-up needs its worker task_id")
            if self.status_after is None:
                raise ValueError(f"follow-up {self.follow_up_id}: an internal follow-up always has a result")
        elif self.round != 1:
            raise ValueError(f"follow-up {self.follow_up_id}: there is only one merchant round")
        return self


class FlagChecks(_Model):
    """The checks behind one memo flag (memo.FlagOutcome has no room for them)."""

    flag: FlagReason
    check_ids: list[str] = Field(default_factory=list)


class DocumentCheck(_Model):
    """The check a memo.DocumentRequest would settle."""

    type: DocumentType
    check_id: str = Field(pattern=CHECK_ID)


class LeadReport(_Model):
    """The Lead's working detail for one review. Filled by code, never by the LLM."""

    case_id: str = Field(pattern=CASE_ID)
    version: int = Field(default=1, ge=1)          # memo version: 2 after step 9 sent it back
    generated_at: datetime
    route: Route = "reviewed"
    route_reason: Optional[str] = None
    checks: list[CheckOutcome] = Field(default_factory=list)
    confidence: Confidence
    provisional: bool = False
    recheck_after: Optional[date] = None
    recheck_targets: list[Literal["registry", "tin_match"]] = Field(default_factory=list)
    follow_ups: list[FollowUpRecord] = Field(default_factory=list)
    flag_checks: list[FlagChecks] = Field(default_factory=list)
    document_checks: list[DocumentCheck] = Field(default_factory=list)

    def check(self, check_id: str) -> CheckOutcome:
        return next(c for c in self.checks if c.check_id == check_id)

    def open_checks(self) -> list[CheckOutcome]:
        """Required checks that are neither AGREE nor EXPLAINED."""
        return [c for c in self.checks if c.required and c.status not in PASSING_STATUSES]

    def cited_ids(self) -> set[str]:
        ids: set[str] = set()
        for c in self.checks:
            ids.update(c.evidence)
            ids.update(c.explanation_evidence)
        for fu in self.follow_ups:
            ids.update(fu.new_evidence)
        return ids

    @model_validator(mode="after")
    def _consistent(self) -> "LeadReport":
        known = [c.check_id for c in self.checks]
        if len(known) != len(set(known)):
            raise ValueError("duplicate check_id in checks")
        referenced = {cid for f in self.flag_checks for cid in f.check_ids}
        referenced |= {fu.check_id for fu in self.follow_ups}
        referenced |= {d.check_id for d in self.document_checks}
        unknown = sorted(referenced - set(known))
        if unknown:
            raise ValueError(f"references to checks that aren't in checks: {unknown}")

        fu_ids = [fu.follow_up_id for fu in self.follow_ups]
        if len(fu_ids) != len(set(fu_ids)):
            raise ValueError("duplicate follow_up_id")
        if sum(1 for fu in self.follow_ups if fu.target == "merchant") > 1:
            raise ValueError("at most one merchant follow-up")

        if self.route != "reviewed" and not self.route_reason:
            raise ValueError(f"route {self.route} needs a route_reason")

        if self.provisional:
            if not self.recheck_after or not self.recheck_targets:
                raise ValueError("a provisional clear needs recheck_after and recheck_targets")
            if not any(c.expected_absence for c in self.checks):
                raise ValueError("a provisional clear rests on a check with expected_absence")
        elif self.recheck_after or self.recheck_targets:
            raise ValueError("recheck_after and recheck_targets are only for a provisional clear")

        required = [c.confidence for c in self.checks if c.required]
        if required and self.confidence != weakest(required):
            raise ValueError(
                f"confidence must be the weakest required check's ({weakest(required)}), not {self.confidence}"
            )
        return self


class LeadCheckFailure(_Model):
    rule: LeadRule
    flag: Optional[FlagReason] = None
    detail: str


# ---------- What GET /reviews/{review_id} returns ----------

class ReviewOutput(_Model):
    """The checked memo (memo.py, unchanged) plus the Lead's report."""

    checked_memo: Optional[CheckedMemo] = None
    lead: LeadReport
    lead_check_failures: list[LeadCheckFailure] = Field(default_factory=list)
    check_attempts: int = Field(default=1, ge=1)
    # Excerpts for IDs the report cites but the memo doesn't (memo.CheckedMemo only
    # indexes what the memo cites). Built by code, like evidence_index.
    report_evidence_index: dict[str, EvidenceEntry] = Field(default_factory=dict)

    @property
    def memo(self):
        return self.checked_memo.memo if self.checked_memo else None

    @property
    def recommendation(self) -> Recommendation:
        return self.memo.recommendation if self.memo else "insufficient_evidence"

    @property
    def passed(self) -> bool:
        memo_ok = self.checked_memo is None or self.checked_memo.check_result.passed
        return memo_ok and not self.lead_check_failures

    @model_validator(mode="after")
    def _consistent(self) -> "ReviewOutput":
        lead = self.lead
        memo = self.memo
        if memo is None:
            if lead.route != "human_queue":
                raise ValueError("only a human_queue review can come without a memo")
        else:
            if memo.case_id != lead.case_id:
                raise ValueError("the memo and the report are for different cases")
            if lead.route == "needs_info" and memo.recommendation != "request_documents":
                raise ValueError("route needs_info recommends request_documents")
            if lead.route == "human_queue" and memo.recommendation != "insufficient_evidence":
                raise ValueError("route human_queue recommends insufficient_evidence")
            if {FlagReason(f.flag) for f in lead.flag_checks} - {FlagReason(f.flag) for f in memo.flags}:
                raise ValueError("flag_checks names a flag the memo doesn't list")
            if {d.type for d in lead.document_checks} - {d.type for d in memo.document_request}:
                raise ValueError("document_checks names a document the memo doesn't request")
            if lead.provisional and memo.recommendation != "recommend_clear":
                raise ValueError("a provisional clear recommends recommend_clear")
            if memo.recommendation == "recommend_clear":
                still_open = [c.check_id for c in lead.open_checks()]
                if still_open:
                    raise ValueError(f"recommend_clear needs every required check AGREE or EXPLAINED; open: {still_open}")
                if any(f.status == "unresolved" for f in memo.flags):
                    raise ValueError("recommend_clear with an unresolved flag")

        # The one safety rule that can't be broken: a HARD_FAIL means escalate.
        if any(c.status == "HARD_FAIL" for c in lead.checks) and self.recommendation != "escalate":
            raise ValueError("a HARD_FAIL check means the recommendation must be escalate")

        bad_keys = _bad_ids(list(self.report_evidence_index))
        if bad_keys:
            raise ValueError(f"report_evidence_index keys must be evidence IDs: {bad_keys}")
        extra = sorted(set(self.report_evidence_index) - lead.cited_ids())
        if extra:
            raise ValueError(f"report_evidence_index holds IDs the report doesn't cite: {extra}")
        # No full TIN check here: like memo.py, a review that breaks a hard rule still
        # validates, and step 9 reports it (memo.CheckRule NO_FULL_TAX_ID).
        return self


__all__ = [
    "CHECK_ID",
    "FLAG_EDGE_CASE",
    "PASSING_STATUSES",
    "CheckOutcome",
    "CheckStatus",
    "CheckStatusValue",
    "DocumentCheck",
    "EdgeCase",
    "FlagChecks",
    "FollowUpRecord",
    "HardFailReason",
    "LeadCheckFailure",
    "LeadReport",
    "LeadRule",
    "ReviewOutput",
    "Route",
    "weakest",
]
