"""Contract: the lead's memo (step 8), and what Checks adds to it (step 9).

- ``Memo`` is the lead agent's structured output: for each open flag, the outcome,
  the root cause and the evidence IDs behind it; the causes ruled out; one
  recommendation; the documents to request; notes for the analyst.
- ``CheckResult`` is what Checks (code) reports after applying the hard rules.
- ``evidence_index`` maps every evidence ID the memo cites to its source, type,
  excerpt and date (``EvidenceEntry``). Checks builds it in code from ``CaseInput``
  and the workers' ``WorkerResult.sources``, so excerpts never come from the model.
- ``CheckedMemo`` holds the three together: what step 10 shows the analyst, and what
  the review app gets from ``GET /reviews/{review_id}``.

Consumers: Checks (step 9), the review app, and the case service, which saves the
recommendation as ``DecisionRecord.agent_recommendation`` (decision.py).

Rules for this contract:
- Structure only. The hard rules (clearing a flag needs both kinds of evidence; an
  EIN that belongs to an unrelated company forces an escalation; a TIN flag can't be
  cleared while the EIN is Not Issued; no full SSN or EIN; every cited ID must exist)
  are applied by Checks to a valid memo and reported in ``CheckResult``, so the lead
  gets the memo back once to revise. A memo that breaks a hard rule still validates.
- The lead writes evidence IDs only, never excerpts.
- ``auto_resolved`` and ``out_of_scope`` are copied from intake by code, not written
  by the model (to confirm).
"""

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .case_input import CASE_ID, Contract, DocumentType, FlagReason
from .worker_io import EVIDENCE_ID

EvidenceId = Annotated[str, Field(pattern=EVIDENCE_ID)]    # R1, K1, D1, B1, W1


# ---------- The memo (written by the lead, step 8) ----------

Recommendation = Literal[
    "recommend_clear",        # every open flag is resolved, with evidence
    "request_documents",      # a document from the merchant would settle it; document_request says which
    "escalate",               # a risk signal, such as an EIN that belongs to an unrelated company
    "insufficient_evidence",  # conflicts remain after the follow-up rounds
]

FlagStatus = Literal["resolved", "unresolved"]


class FlagOutcome(Contract):
    """The lead's conclusion on one flag that intake left open."""

    flag: FlagReason
    status: FlagStatus
    root_cause: str | None = None                  # what explains the flag; required when resolved
    evidence: list[EvidenceId] = Field(default_factory=list)

    @model_validator(mode="after")
    def resolved_needs_cause_and_evidence(self) -> "FlagOutcome":
        if self.status == "resolved" and not (self.root_cause and self.evidence):
            raise ValueError(f"{self.flag.value}: a resolved flag needs a root_cause and at least one evidence ID")
        return self


class RuledOutCause(Contract):
    """A cause the lead checked and excluded, with the evidence that excludes it."""

    cause: str
    evidence: list[EvidenceId] = Field(min_length=1)


class DocumentRequest(Contract):
    """One document to ask the merchant for."""

    type: DocumentType
    reason: str                                    # what the document would settle, in plain words


class Memo(Contract):
    """The lead agent's memo (step 8). Every case flag appears in exactly one of
    flags, auto_resolved and out_of_scope."""

    case_id: str = Field(pattern=CASE_ID)
    flags: list[FlagOutcome] = Field(default_factory=list)          # the flags intake left open
    auto_resolved: list[FlagReason] = Field(default_factory=list)   # formatting differences intake resolved
    out_of_scope: list[FlagReason] = Field(default_factory=list)    # flags the agent doesn't review
    ruled_out: list[RuledOutCause] = Field(default_factory=list)
    recommendation: Recommendation
    document_request: list[DocumentRequest] = Field(default_factory=list)
    notes_for_analyst: str = ""

    @model_validator(mode="after")
    def check_structure(self) -> "Memo":
        named = [f.flag for f in self.flags] + self.auto_resolved + self.out_of_scope
        if not named:
            raise ValueError("the memo covers no flag; list each of the case's flags once")
        duplicates = sorted({f.value for f in named if named.count(f) > 1})
        if duplicates:
            raise ValueError(f"each flag goes in one place only (flags, auto_resolved or out_of_scope): {duplicates}")
        if self.recommendation == "request_documents" and not self.document_request:
            raise ValueError("recommendation request_documents needs at least one document_request")
        return self

    def cited_ids(self) -> set[str]:
        """Every evidence ID the memo cites. Checks builds evidence_index from these."""
        ids = {i for outcome in self.flags for i in outcome.evidence}
        return ids | {i for cause in self.ruled_out for i in cause.evidence}


# ---------- What Checks adds (code, step 9) ----------

EvidenceType = Literal[
    "official_record",     # R: a state registry, county clerk or IRS record
    "third_party_record",  # R: a vendor record, such as the commercial database
    "owner_kyc",           # K: the KYC vendor's result for an owner
    "merchant_document",   # D
    "bank_verification",   # B
    "website",             # W
]

# The types each evidence-ID prefix can have.
TYPES_BY_PREFIX: dict[str, frozenset[str]] = {
    "R": frozenset({"official_record", "third_party_record"}),
    "K": frozenset({"owner_kyc"}),
    "D": frozenset({"merchant_document"}),
    "B": frozenset({"bank_verification"}),
    "W": frozenset({"website"}),
}


class EvidenceEntry(Contract):
    """One cited ID in evidence_index, built by Checks from the saved source."""

    source: str                                    # e.g. "IL SoS filing history", "Merchant upload", the page URL
    type: EvidenceType
    excerpt: str = Field(min_length=1)
    as_of: date | None = None                      # the record's date; a KYC result has none


CheckRule = Literal[
    "CLEAR_NEEDS_TWO_SOURCES",      # a resolved flag cites R or K, and D, B or W
    "UNRELATED_EIN_MUST_ESCALATE",  # an EIN that belongs to an unrelated company forces escalate
    "NOT_ISSUED_TIN_CANT_CLEAR",    # a TIN flag can't be resolved while the IRS says Not Issued
    "NO_FULL_TAX_ID",               # no full SSN or EIN anywhere in the memo
    "CITED_ID_MUST_EXIST",          # every cited ID is in CaseInput or the workers' sources
]


class CheckFailure(Contract):
    rule: CheckRule
    flag: FlagReason | None = None                 # the flag it concerns, if any
    evidence: list[EvidenceId] = Field(default_factory=list)  # the IDs involved, e.g. one that doesn't exist
    detail: str


class CheckResult(Contract):
    """What Checks (step 9) reports for one memo."""

    passed: bool
    failures: list[CheckFailure] = Field(default_factory=list)

    @model_validator(mode="after")
    def passed_means_no_failures(self) -> "CheckResult":
        if self.passed == bool(self.failures):
            raise ValueError("passed must be true exactly when there are no failures")
        return self


class CheckedMemo(Contract):
    """A memo after Checks: what the analyst sees at step 10.

    evidence_index holds only the IDs the memo cites. A cited ID missing from it is a
    citation error, so it needs a CITED_ID_MUST_EXIST failure in check_result. After
    the one revision, a memo can still reach the analyst with failed checks; the
    review app then shows the failures as a warning.
    """

    memo: Memo
    evidence_index: dict[EvidenceId, EvidenceEntry]
    check_result: CheckResult

    @model_validator(mode="after")
    def index_matches_citations(self) -> "CheckedMemo":
        misfits = sorted(i for i, entry in self.evidence_index.items() if entry.type not in TYPES_BY_PREFIX[i[0]])
        if misfits:
            raise ValueError(f"evidence_index types don't fit their IDs: {misfits}")
        cited = self.memo.cited_ids()
        uncited = sorted(set(self.evidence_index) - cited)
        if uncited:
            raise ValueError(f"evidence_index holds IDs the memo doesn't cite: {uncited}")
        reported = {i for f in self.check_result.failures if f.rule == "CITED_ID_MUST_EXIST" for i in f.evidence}
        unexplained = sorted(cited - set(self.evidence_index) - reported)
        if unexplained:
            raise ValueError(f"cited IDs missing from evidence_index without a CITED_ID_MUST_EXIST failure: {unexplained}")
        return self
