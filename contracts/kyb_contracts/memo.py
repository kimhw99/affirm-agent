"""Contract: Memo, CheckResult, evidence_index (steps 8, 9 and 10).

Producer: the agent service (H). Consumer: the review app (XL), and evaluation.

What the review app gets from ``GET /reviews/{review_id}`` once the status is
``awaiting_decision`` is a ``ReviewOutput``:

- ``memo``: written in step 8. Code fills the structured parts (the per-check statuses,
  the recommendation, confidence, ``auto_resolved``, ``out_of_scope``, the follow-up
  history); the LLM writes only the prose (root causes, explanations, notes) and the
  evidence IDs that back it. Evidence IDs only, never excerpts.
- ``evidence_index``: built by code in step 9 from ``CaseInput`` and the workers'
  ``WorkerResult.sources``, one entry per ID the memo cites. The UI uses it to show
  each citation's excerpt.
- ``check_result``: the step 9 guardrail result ("checks passed" or a warning).

Two names that are easy to mix up:

- ``CheckOutcome`` is one *verification check* from the Lead's plan (for example
  "the DBA belongs to the legal entity") and its reconcile status.
- ``CheckResult`` is the step 9 *guardrail* result for the whole memo.

How the brief's outcomes map onto the four ``Recommendation`` values (which
``decision.AGREES_WITH`` already uses):

| Outcome                                   | recommendation          | other fields                         |
|-------------------------------------------|-------------------------|--------------------------------------|
| clear                                     | recommend_clear         |                                      |
| provisional clear (EC-4 expected absence) | recommend_clear         | provisional=True, recheck_after      |
| any HARD_FAIL (escalate or reject)        | escalate                | hard_fail_reason on the check        |
| conflict needs a merchant document        | request_documents       | document_request                     |
| intake: needs_info (inputs missing)       | request_documents       | route="needs_info", document_request |
| intake: no/unknown flags, bad payload     | insufficient_evidence   | route="human_queue", route_reason    |
| conflicts left open after follow-ups      | insufficient_evidence   | open checks listed in ``checks``     |

Rules this module enforces (structure and internal consistency only, like the other
contracts; the business rules live in step 9's checks), plus one safety rule that must
be impossible to break: a memo with a HARD_FAIL check can't recommend anything but
escalate.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kyb_contracts.case_input import CASE_ID, TIN_PATTERN, DocumentType, FlagReason
from kyb_contracts.worker_io import EVIDENCE_ID, Authority, Confidence

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
    "document_tampered",
    "other",
]

Recommendation = Literal[
    "recommend_clear", "request_documents", "escalate", "insufficient_evidence"
]

Route = Literal["reviewed", "needs_info", "human_queue"]

# What the Lead may ask the merchant for: any case document type, plus a bank letter.
RequestableDocument = Union[DocumentType, Literal["bank_letter"]]

GuardrailRule = Literal[
    "CITATION_NOT_FOUND",
    "CLEAR_NEEDS_TWO_SOURCES",
    "EIN_OTHER_ENTITY_NOT_ESCALATED",
    "NOT_ISSUED_TIN_CLEARED",
    "HARD_FAIL_NOT_ESCALATED",
    "REQUIRED_CHECK_MISSING",
    "DOCUMENT_ALREADY_IN_CASE",
    "FULL_TIN_IN_MEMO",
    "WEBSITE_OVERRULES_RECORDS",
]

_CONFIDENCE_RANK: dict[str, int] = {
    Confidence.LOW.value: 0,
    Confidence.MEDIUM.value: 1,
    Confidence.HIGH.value: 2,
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


def weakest(levels: list[str]) -> str:
    """Overall confidence is the weakest check's, never an average."""
    return min(levels, key=_CONFIDENCE_RANK.__getitem__)


def _is_evidence_id(value: str) -> bool:
    return bool(_EVIDENCE_ID_RE.match(value))


# ---------- Memo parts ----------

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
        for eid in self.evidence + self.explanation_evidence:
            if not _is_evidence_id(eid):
                raise ValueError(f"check {self.check_id}: {eid!r} is not an evidence ID")
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


class FlagOutcome(_Model):
    """The outcome for one in-scope flag the Lead worked on."""

    flag: FlagReason
    status: Literal["resolved", "unresolved"]
    root_cause: str = Field(min_length=1)
    check_ids: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ids(self) -> "FlagOutcome":
        if FlagReason(self.flag) not in FLAG_EDGE_CASE:
            raise ValueError(f"{self.flag} is out of scope; list it in Memo.out_of_scope")
        bad = [e for e in self.evidence if not _is_evidence_id(e)]
        if bad:
            raise ValueError(f"flag {self.flag}: not evidence IDs: {bad}")
        return self


class RuledOut(_Model):
    cause: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _ids(self) -> "RuledOut":
        bad = [e for e in self.evidence if not _is_evidence_id(e)]
        if bad:
            raise ValueError(f"ruled_out: not evidence IDs: {bad}")
        return self


class FollowUpRecord(_Model):
    """One targeted follow-up and what came of it, kept for the memo's history."""

    follow_up_id: str = Field(pattern=FOLLOW_UP_ID)
    check_id: str = Field(pattern=CHECK_ID)
    target: Literal["records", "evidence", "merchant"]
    round: int = Field(ge=1)
    ask: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    task_id: Optional[str] = None
    status_before: CheckStatusValue
    status_after: Optional[CheckStatusValue] = None
    new_evidence: list[str] = Field(default_factory=list)
    sent_at: datetime
    answered_at: Optional[datetime] = None

    @model_validator(mode="after")
    def _shape(self) -> "FollowUpRecord":
        bad = [e for e in self.new_evidence if not _is_evidence_id(e)]
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


class DocumentRequest(_Model):
    """One document to ask the merchant for, tied to the check it would settle."""

    document_type: RequestableDocument
    check_id: str = Field(pattern=CHECK_ID)
    reason: str = Field(min_length=1)


class Memo(_Model):
    """The Lead's memo for one case. Evidence IDs only, never excerpts."""

    case_id: str = Field(pattern=CASE_ID)
    version: int = Field(default=1, ge=1)
    generated_at: datetime
    route: Route = "reviewed"
    route_reason: Optional[str] = None
    flags: list[FlagOutcome] = Field(default_factory=list)
    auto_resolved: list[FlagReason] = Field(default_factory=list)
    out_of_scope: list[FlagReason] = Field(default_factory=list)
    checks: list[CheckOutcome] = Field(default_factory=list)
    ruled_out: list[RuledOut] = Field(default_factory=list)
    recommendation: Recommendation
    confidence: Confidence
    provisional: bool = False
    recheck_after: Optional[date] = None
    recheck_targets: list[Literal["registry", "tin_match"]] = Field(default_factory=list)
    document_request: list[DocumentRequest] = Field(default_factory=list)
    follow_ups: list[FollowUpRecord] = Field(default_factory=list)
    notes_for_analyst: str = ""

    def cited_ids(self) -> set[str]:
        """Every evidence ID the memo cites; step 9 builds evidence_index from these."""
        ids: set[str] = set()
        for f in self.flags:
            ids.update(f.evidence)
        for c in self.checks:
            ids.update(c.evidence)
            ids.update(c.explanation_evidence)
        for r in self.ruled_out:
            ids.update(r.evidence)
        for fu in self.follow_ups:
            ids.update(fu.new_evidence)
        return ids

    def open_checks(self) -> list[CheckOutcome]:
        """Required checks that are neither AGREE nor EXPLAINED."""
        return [c for c in self.checks if c.required and c.status not in PASSING_STATUSES]

    @model_validator(mode="after")
    def _consistent(self) -> "Memo":
        known = [c.check_id for c in self.checks]
        if len(known) != len(set(known)):
            raise ValueError("duplicate check_id in checks")

        referenced = {cid for f in self.flags for cid in f.check_ids}
        referenced |= {fu.check_id for fu in self.follow_ups}
        referenced |= {d.check_id for d in self.document_request}
        unknown = sorted(referenced - set(known))
        if unknown:
            raise ValueError(f"references to checks that aren't in checks: {unknown}")

        fu_ids = [fu.follow_up_id for fu in self.follow_ups]
        if len(fu_ids) != len(set(fu_ids)):
            raise ValueError("duplicate follow_up_id")
        if sum(1 for fu in self.follow_ups if fu.target == "merchant") > 1:
            raise ValueError("at most one merchant follow-up")

        flagged = [f.flag for f in self.flags]
        if len(flagged) != len(set(flagged)):
            raise ValueError("a flag appears twice in flags")
        overlap = sorted(
            (set(flagged) & set(self.auto_resolved))
            | (set(flagged) & set(self.out_of_scope))
            | (set(self.auto_resolved) & set(self.out_of_scope))
        )
        if overlap:
            raise ValueError(
                f"a flag is listed in more than one of flags/auto_resolved/out_of_scope: {overlap}"
            )

        if self.route != "reviewed" and not self.route_reason:
            raise ValueError(f"route {self.route} needs a route_reason")
        if self.route == "needs_info" and self.recommendation != "request_documents":
            raise ValueError("route needs_info recommends request_documents")
        if self.route == "human_queue" and self.recommendation != "insufficient_evidence":
            raise ValueError("route human_queue recommends insufficient_evidence")
        if self.route == "reviewed" and self.flags and not self.checks:
            raise ValueError("a reviewed memo with flags needs its checks")

        # The one safety rule that can't be broken: a HARD_FAIL means escalate.
        if any(c.status == "HARD_FAIL" for c in self.checks) and self.recommendation != "escalate":
            raise ValueError("a HARD_FAIL check means the recommendation must be escalate")

        if self.recommendation == "recommend_clear":
            still_open = [c.check_id for c in self.open_checks()]
            if still_open:
                raise ValueError(
                    "recommend_clear needs every required check AGREE or EXPLAINED; "
                    f"open: {still_open}"
                )
            if any(f.status == "unresolved" for f in self.flags):
                raise ValueError("recommend_clear with an unresolved flag")

        if self.provisional:
            if self.recommendation != "recommend_clear" or not self.recheck_after or not self.recheck_targets:
                raise ValueError(
                    "a provisional clear is recommend_clear with recheck_after and recheck_targets"
                )
            if not any(c.expected_absence for c in self.checks):
                raise ValueError("a provisional clear rests on a check with expected_absence")
        elif self.recheck_after or self.recheck_targets:
            raise ValueError("recheck_after and recheck_targets are only for a provisional clear")

        if self.recommendation == "request_documents" and not self.document_request:
            raise ValueError("request_documents needs at least one document_request")

        required = [c.confidence for c in self.checks if c.required]
        if required:
            expected = weakest(required)
            if self.confidence != expected:
                raise ValueError(
                    f"confidence must be the weakest required check's ({expected}), "
                    f"not {self.confidence}"
                )

        if TIN_PATTERN.search(self.model_dump_json()):
            raise ValueError("a full SSN or EIN appears in the memo; use the last 4 digits only")
        return self


# ---------- Step 9 output ----------

class EvidenceEntry(_Model):
    """What the UI shows for one cited ID. Built by code from saved sources, never by the LLM."""

    source: str
    type: Literal[
        "official_record",
        "third_party_record",
        "owner_kyc",
        "merchant_document",
        "bank_verification",
        "website",
    ]
    authority: Authority
    locator: str
    excerpt: str
    as_of: Optional[date] = None


EvidenceIndex = dict[str, EvidenceEntry]


class CheckFailure(_Model):
    rule: GuardrailRule
    flag: Optional[FlagReason] = None
    detail: str


class CheckResult(_Model):
    """The step 9 guardrail result for the memo."""

    passed: bool
    failures: list[CheckFailure] = Field(default_factory=list)
    attempts: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _passed_means_no_failures(self) -> "CheckResult":
        if self.passed != (not self.failures):
            raise ValueError("passed must be True exactly when there are no failures")
        return self


class ReviewOutput(_Model):
    """What GET /reviews/{review_id} carries once the review is awaiting a decision."""

    memo: Memo
    evidence_index: EvidenceIndex = Field(default_factory=dict)
    check_result: CheckResult

    @model_validator(mode="after")
    def _index_matches_memo(self) -> "ReviewOutput":
        bad_keys = sorted(k for k in self.evidence_index if not _is_evidence_id(k))
        if bad_keys:
            raise ValueError(f"evidence_index keys must be evidence IDs: {bad_keys}")
        cited = self.memo.cited_ids()
        extra = sorted(set(self.evidence_index) - cited)
        if extra:
            raise ValueError(f"evidence_index holds IDs the memo doesn't cite: {extra}")
        missing = cited - set(self.evidence_index)
        reported = {
            token
            for f in self.check_result.failures
            if f.rule == "CITATION_NOT_FOUND"
            for token in re.findall(r"[RDBWK]\d+", f.detail)
        }
        unreported = sorted(missing - reported)
        if unreported:
            raise ValueError(
                f"cited IDs missing from evidence_index without a CITATION_NOT_FOUND failure: {unreported}"
            )
        if TIN_PATTERN.search(self.model_dump_json()):
            raise ValueError("a full SSN or EIN appears in the review output")
        return self


__all__ = [
    "CHECK_ID",
    "FLAG_EDGE_CASE",
    "PASSING_STATUSES",
    "CheckFailure",
    "CheckOutcome",
    "CheckResult",
    "CheckStatus",
    "CheckStatusValue",
    "DocumentRequest",
    "EdgeCase",
    "EvidenceEntry",
    "EvidenceIndex",
    "FlagOutcome",
    "FollowUpRecord",
    "GuardrailRule",
    "HardFailReason",
    "Memo",
    "Recommendation",
    "RequestableDocument",
    "ReviewOutput",
    "Route",
    "RuledOut",
    "weakest",
]
