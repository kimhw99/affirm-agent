"""Contract: messages between the lead agent and its two workers.

The lead sends a ``WorkerInput`` (also called ``WorkerTask``, or ``FollowUp`` when
``follow_up_of`` is set). A worker always returns one ``WorkerResult``.

Producers:
- ``WorkerInput`` is produced by the lead agent.
- ``WorkerResult`` (and every ``Finding`` / ``Source`` in it) is produced by the
  Records and Evidence workers.

Rules this module enforces:
- Every finding cites a source that is listed in ``sources``.
- Every finding id named in ``hypothesis_signals`` exists in ``findings``.
- A worker never recommends approve or deny; it reports findings and signals only.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    THIRD_PARTY = "third_party"  # vendors: KYC provider, Middesk
    MERCHANT = "merchant"        # uploads and the merchant's own website


class ExtractionMethod(str, Enum):
    STRUCTURED_RECORD = "structured_record"      # a field read from a database record
    DOCUMENT_EXTRACTION = "document_extraction"  # a field read from an uploaded document
    PAGE_TEXT = "page_text"                      # read from website text (untrusted, low confidence)
    DERIVED = "derived"                          # computed by code, e.g. a name comparison


WorkerName = Literal["records", "evidence"]
WorkerStatus = Literal["completed", "partial", "failed"]
SignalDirection = Literal["supports", "contradicts", "neutral"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


# ==========================================
# Shared & Nested Models
# ==========================================

class Hypothesis(_Model):
    hypothesis_id: str
    text: str


class Owner(_Model):
    owner_id: str
    name: str
    role: str
    kyc_ref: Optional[str] = None


class Document(_Model):
    doc_id: str
    doc_type: str
    file_ref: str
    uploaded_at: datetime


class CaseContext(_Model):
    case_id: str
    ec_flags: list[str]
    submitted_legal_name: str
    entity_type: str
    formation_state: str = Field(min_length=2, max_length=2)
    address: str
    tin_ref: str
    tin_last4: str = Field(pattern=r"^\d{4}$")
    owners: list[Owner]
    documents: list[Document] = Field(default_factory=list)
    website: Optional[str] = None


class Citation(_Model):
    source_id: str
    locator: str
    excerpt: str = Field(min_length=1)


class Finding(_Model):
    finding_id: str
    subject: str  # "business" or "owner:<owner_id>"
    field: str
    value: Any
    source_id: str
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
    source_id: str
    source_type: str
    authority: Authority
    locator: str
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


# Names used in the README folder guide.
WorkerTask = WorkerInput
FollowUp = WorkerInput


class WorkerResult(_Model):
    """What a worker returns to the lead."""

    task_id: str
    case_id: str
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
    "CaseContext",
    "Citation",
    "Confidence",
    "Document",
    "ExtractionMethod",
    "Finding",
    "FollowUp",
    "Hypothesis",
    "HypothesisSignal",
    "Owner",
    "SignalDirection",
    "Source",
    "WorkerInput",
    "WorkerName",
    "WorkerResult",
    "WorkerStatus",
    "WorkerTask",
]
