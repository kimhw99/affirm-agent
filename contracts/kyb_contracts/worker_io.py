"""Contract: messages between the lead agent and its two workers.

The lead sends a ``WorkerInput`` (also called ``WorkerTask``, or ``FollowUp`` when
``follow_up_of`` is set). A worker always returns one ``WorkerResult``.

Producers:
- ``WorkerInput`` is produced by the lead agent from a ``CaseInput`` (case_input.py).
- ``WorkerResult`` (and every ``Finding`` / ``Source`` in it) is produced by the
  Records and Evidence workers.

How this lines up with case_input.py:
- Everything (the application, the vendor checks, document fields, bank
  verification, web snapshots, KYC results) is already parsed and stored by the
  case service. Workers parse nothing. ``CaseContext`` carries the submitted facts
  and the *keys* (evidence IDs D/B/W/K, owner refs ``owner:P1``); the workers'
  tools fetch the stored records by those keys.
- The ID formats, enums, ``Application`` (with its owners) and the vendor ``Checks``
  are imported from case_input.py, never copied, so the two files can't drift apart.
- A source's ``source_id`` *is* its evidence ID: ``D1``/``B1``/``W1``/``K1`` for case
  evidence (fixed when the case arrives) and ``R<n>`` for an official or
  third-party record the Records worker looked up. The memo and Checks cite these.

Rules this module enforces:
- No full TIN, SSN or EIN anywhere in the context; only the last 4 digits.
- Every finding cites a source listed in ``sources``, and every finding id named
  in ``hypothesis_signals`` exists in ``findings``.
- Trust boundary: the Records worker cites only R and K sources; the Evidence
  worker cites only D, B and W sources.
- A worker never recommends approve or deny; it reports findings and signals only.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kyb_contracts.case_input import (
    BANK_ID,
    CASE_ID,
    DOC_ID,
    KYC_ID,
    OWNER_REF,
    TIN_PATTERN,
    WEB_ID,
    Application,
    Checks,
    DocumentType,
    EntityType,
    FlagReason,
)

# ==========================================
# ID formats (case IDs come from case_input.py)
# ==========================================

RECORD_ID = r"^R\d+$"                      # assigned by the Records worker
EVIDENCE_ID = r"^[RDBWK]\d+$"
SUBJECT = r"^(business|owner:P\d+)$"       # what a finding is about

# Which evidence-ID prefixes each worker may cite (the trust boundary).
WORKER_SOURCE_PREFIXES: dict[str, frozenset[str]] = {
    "records": frozenset({"R", "K"}),       # official records, vendors, owner KYC
    "evidence": frozenset({"D", "B", "W"}), # documents, bank verification, website
}


# ==========================================
# Enums
# ==========================================

class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Authority(str, Enum):
    """Who controls the data behind a source (the trust boundary)."""

    OFFICIAL = "official"        # government: SOS registry, county clerk, IRS
    THIRD_PARTY = "third_party"  # vendors: KYC provider, bank verification, commercial databases
    MERCHANT = "merchant"        # uploaded documents and the merchant's own website


class ExtractionMethod(str, Enum):
    STRUCTURED_RECORD = "structured_record"      # a field of an official or vendor record
    DOCUMENT_EXTRACTION = "document_extraction"  # a field the existing system extracted from an uploaded document
    PAGE_TEXT = "page_text"                      # a field from a website snapshot (untrusted, low confidence)
    DERIVED = "derived"                          # computed by code, e.g. a name comparison


WorkerName = Literal["records", "evidence"]
WorkerStatus = Literal["completed", "partial", "failed"]
SignalDirection = Literal["supports", "contradicts", "neutral"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


# ==========================================
# Case context: submitted facts + keys
# ==========================================

class Hypothesis(_Model):
    hypothesis_id: str
    text: str


class DocumentRef(_Model):
    """Key for one ``case_input.Document``; the Evidence worker fetches its stored fields."""

    id: str = Field(pattern=DOC_ID)
    type: DocumentType


class BankVerificationRef(_Model):
    """Key for ``case_input.BankVerification``."""

    id: str = Field(pattern=BANK_ID)


class WebSnapshotRef(_Model):
    """Key for one ``case_input.WebSnapshot``."""

    id: str = Field(pattern=WEB_ID)
    url: str
    captured_at: date


class OwnerKycRef(_Model):
    """Key for one ``case_input.OwnerKYC``; the Records worker fetches the result."""

    id: str = Field(pattern=KYC_ID)
    owner_ref: str = Field(pattern=OWNER_REF)


class CaseContext(_Model):
    """What both workers know about the case. Mirrors ``CaseInput`` field by field, minus
    the stored values the workers must fetch themselves."""

    case_id: str = Field(pattern=CASE_ID)
    flag_reasons: list[FlagReason] = Field(min_length=1)
    application: Application                             # case_input.Application, unchanged
    checks: Checks                                       # the vendor's labels (e.g. Middesk), as received
    documents: list[DocumentRef] = Field(default_factory=list)
    bank_verification: Optional[BankVerificationRef] = None
    web_snapshot: list[WebSnapshotRef] = Field(default_factory=list)
    owner_kyc: list[OwnerKycRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _integrity(self) -> "CaseContext":
        ids = [d.id for d in self.documents] + [w.id for w in self.web_snapshot]
        ids += [k.id for k in self.owner_kyc]
        if self.bank_verification:
            ids.append(self.bank_verification.id)
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate evidence IDs: {duplicates}")

        owner_refs = {o.ref for o in self.application.owners}
        unknown = sorted({k.owner_ref for k in self.owner_kyc} - owner_refs)
        if unknown:
            raise ValueError(f"owner_kyc refers to unknown owners: {unknown}")

        # state_entity_id is skipped: some states issue 9-digit entity IDs.
        if TIN_PATTERN.search(self.model_dump_json(exclude={"application": {"state_entity_id"}})):
            raise ValueError("a full SSN/EIN appears in the context; pass only tin_last4")
        return self

    def evidence_ids(self) -> set[str]:
        ids = {d.id for d in self.documents} | {w.id for w in self.web_snapshot}
        ids |= {k.id for k in self.owner_kyc}
        if self.bank_verification:
            ids.add(self.bank_verification.id)
        return ids


# ==========================================
# Findings and sources
# ==========================================

class Citation(_Model):
    source_id: str = Field(pattern=EVIDENCE_ID)
    locator: str            # where inside the source, e.g. "NY DOS #7812345" or "D1.extracted.name"
    excerpt: str = Field(min_length=1)


class Finding(_Model):
    finding_id: str
    subject: str = Field(pattern=SUBJECT)   # "business" or an owner ref such as "owner:P1"
    field: str
    value: Any
    source_id: str = Field(pattern=EVIDENCE_ID)
    extraction_method: ExtractionMethod
    confidence: Confidence
    citation: Citation
    note: Optional[str] = None

    @model_validator(mode="after")
    def _citation_matches_source(self) -> "Finding":
        if self.citation.source_id != self.source_id:
            raise ValueError(
                f"finding {self.finding_id}: citation.source_id "
                f"{self.citation.source_id!r} != source_id {self.source_id!r}"
            )
        return self


class Source(_Model):
    """One piece of evidence. ``source_id`` is its evidence ID (R1, D1, B1, W1, K1)."""

    source_id: str = Field(pattern=EVIDENCE_ID)
    source_type: str        # e.g. "sos_registry", "county_dba", "irs_tin_match", "irs_147c", "owner_kyc"
    authority: Authority
    locator: str            # the record's own key, e.g. "NY DOS #7812345" or "case_2001/D1"
    retrieved_at: datetime


class HypothesisSignal(_Model):
    hypothesis_id: str
    direction: SignalDirection
    finding_ids: list[str]


# ==========================================
# Top-Level Worker Schemas
# ==========================================

class WorkerInput(_Model):
    """Task the lead sends to a worker (a first task or a follow-up)."""

    task_id: str
    worker: WorkerName
    question: str
    focus_fields: list[str]
    hypotheses: list[Hypothesis]
    context: CaseContext
    prior_findings: list[Finding] = Field(default_factory=list)
    follow_up_of: Optional[str] = None
    max_tool_calls: int = Field(ge=1, le=20)
    # R IDs must be unique across the whole review; the lead sets this past every R ID used so far.
    record_id_start: int = Field(default=1, ge=1)


# Names used in the README folder guide.
WorkerTask = WorkerInput
FollowUp = WorkerInput


class WorkerResult(_Model):
    """What a worker returns to the lead."""

    task_id: str
    case_id: str = Field(pattern=CASE_ID)
    worker: WorkerName
    status: WorkerStatus
    findings: list[Finding]
    sources: list[Source]
    hypothesis_signals: list[HypothesisSignal]
    not_found: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    answer: Optional[str] = None  # 2-4 sentences answering the task question, citing finding ids
    tool_calls_used: int = Field(ge=0)
    error: Optional[str] = None

    @model_validator(mode="after")
    def _references_resolve(self) -> "WorkerResult":
        source_ids = {s.source_id for s in self.sources}
        finding_ids = {f.finding_id for f in self.findings}

        if len(finding_ids) != len(self.findings):
            raise ValueError("duplicate finding_id in findings")
        if len(source_ids) != len(self.sources):
            raise ValueError("duplicate source_id in sources")

        allowed = WORKER_SOURCE_PREFIXES[self.worker]
        outside = sorted(s for s in source_ids if s[0] not in allowed)
        if outside:
            raise ValueError(f"the {self.worker} worker may not cite {outside} "
                             f"(allowed prefixes: {sorted(allowed)})")

        missing_sources = sorted({f.source_id for f in self.findings} - source_ids)
        if missing_sources:
            raise ValueError(f"findings cite unknown sources: {missing_sources}")

        cited = {fid for sig in self.hypothesis_signals for fid in sig.finding_ids}
        missing_findings = sorted(cited - finding_ids)
        if missing_findings:
            raise ValueError(f"hypothesis_signals cite unknown findings: {missing_findings}")

        if self.status == "failed" and not self.error:
            raise ValueError("status 'failed' requires an error message")
        return self


__all__ = [
    "Authority",
    "BankVerificationRef",
    "CaseContext",
    "Citation",
    "Confidence",
    "DocumentRef",
    "ExtractionMethod",
    "Finding",
    "FollowUp",
    "Hypothesis",
    "HypothesisSignal",
    "OwnerKycRef",
    "SignalDirection",
    "Source",
    "WebSnapshotRef",
    "WORKER_SOURCE_PREFIXES",
    "WorkerInput",
    "WorkerName",
    "WorkerResult",
    "WorkerStatus",
    "WorkerTask",
]
