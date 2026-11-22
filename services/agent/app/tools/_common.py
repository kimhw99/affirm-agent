"""Shared plumbing for the worker tools.

The LLM chooses tools and reads their results; code writes the findings. Each tool:

1. fetches one stored record by a key (an evidence ID from the context, the
   application's state ID, or a name),
2. renders it as plain ``key: value`` lines and registers it as a ``Source``,
3. turns its fields into ``Finding``s whose citation excerpt is one of those lines,
   copied word for word, plus a few findings derived by code (dates, last-4 checks).

Name matching for the *verdict* belongs to the Lead (``app/graph/reconcile.py``): it is
the only side that sees both the records and the merchant's evidence. The tools still
compare names with the application, but only as ``name_checks`` hints in what the
worker's model sees (which lookup to run next), never as findings the Lead could read.

Nothing here parses documents or raw text; the case service already did that.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Iterable, Optional

from pydantic import BaseModel, ConfigDict

from kyb_contracts.case_input import TIN_PATTERN
from kyb_contracts.worker_io import (
    Authority,
    Citation,
    Confidence,
    FINDING_FIELDS,
    ExtractionMethod,
    Finding,
    Source,
    WorkerInput,
    WorkerName,
)

from app.clients.case_client import CaseDataClient
from app.shared.name_norm import LABEL_RANK, NameComparison, best_comparison

# The most R sources (official or third-party records) one tool call may add. Searches
# keep only their closest matches. The lead gives parallel Records follow-ups separate
# blocks of R numbers (app/graph/followup.py, R_ID_STRIDE), sized from this cap.
MAX_RECORDS_PER_CALL = 3

UNTRUSTED_OPEN = "<untrusted_merchant_content>"
UNTRUSTED_CLOSE = "</untrusted_merchant_content>"

# Fields that hold record IDs, never tax IDs: some states issue 9-digit entity IDs.
ID_FIELDS = {"state_entity_id", "registrant_state_entity_id", "event_id", "filing_id", "profile_id"}

REDACTED = "[REDACTED-TIN]"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def redact(text: str) -> str:
    """Mask anything shaped like a full SSN, EIN or 9-digit number (a backstop)."""
    return TIN_PATTERN.sub(REDACTED, text)


def redact_value(field: str, value: Any) -> Any:
    """``redact`` applied to every string inside a finding value, except record-ID fields."""
    if field in ID_FIELDS:
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_value(field, v) for v in value]
    if isinstance(value, dict):
        return {k: redact_value(k, v) for k, v in value.items()}
    return value


def fmt(value: Any) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "; ".join(fmt(v) for v in value) if value else "(none)"
    return str(value)


def as_value(value: Any) -> Any:
    """A JSON-friendly finding value (dates as ISO strings)."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [as_value(v) for v in value]
    return value


def parse_date(value: Any) -> Optional[date]:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def confidence_from_score(score: float) -> str:
    """Extraction confidence (0-1) to a label: >=0.9 high, >=0.7 medium, else low."""
    if score >= 0.9:
        return Confidence.HIGH.value
    if score >= 0.7:
        return Confidence.MEDIUM.value
    return Confidence.LOW.value


_ADDRESS_WORDS = {
    "street": "st", "avenue": "ave", "road": "rd", "boulevard": "blvd", "drive": "dr", "lane": "ln",
    "court": "ct", "place": "pl", "suite": "ste", "highway": "hwy", "parkway": "pkwy",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
}


def normalize_address(address: str) -> str:
    words = re.sub(r"[^0-9a-z]+", " ", address.casefold()).split()
    return " ".join(_ADDRESS_WORDS.get(w, w) for w in words)


def addresses_match(a: str, b: str) -> bool:
    return normalize_address(a) == normalize_address(b)


# --------------------------------------------------------------------------
# Rendered records
# --------------------------------------------------------------------------

class Record:
    """A source rendered as ``key: value`` lines. Citation excerpts are whole lines."""

    def __init__(self, title: str):
        self.lines: list[str] = [title]

    def add(self, key: str, value: Any) -> str:
        text = fmt(value)
        if key.split(".")[-1] not in ID_FIELDS:
            text = redact(text)
        line = f"{key}: {text}"
        self.lines.append(line)
        return line

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def render_record(title: str, fields: Iterable[tuple[str, Any]]) -> tuple[str, dict[str, str]]:
    """Render ``fields`` as lines; returns the text and each key's line."""
    record = Record(title)
    lines = {key: record.add(key, value) for key, value in fields}
    return record.text, lines


# --------------------------------------------------------------------------
# Tool context and output
# --------------------------------------------------------------------------

class ToolInputError(ValueError):
    """The LLM asked for something it may not have (an ID outside its context, a missing key)."""


@dataclass
class ToolOutput:
    tool: str
    sources: list[Source] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    note: Optional[str] = None
    error: Optional[str] = None       # a tool or case-service failure: the worker becomes "partial"
    rejected: bool = False            # the request was refused (bad ID, wrong tool); not a failure
    merchant_sources: set[str] = field(default_factory=set)
    cached: bool = False
    hints: list[str] = field(default_factory=list)   # name checks for the worker's model; not findings

    def llm_view(self) -> str:
        """What the model sees: compact JSON. Merchant values go inside untrusted tags."""
        head: dict[str, Any] = {"tool": self.tool}
        if self.rejected:
            head["rejected"] = self.note
        elif self.note:
            head["note"] = self.note
        if self.error:
            head["error"] = self.error
        if self.sources:
            head["sources"] = [{"source_id": s.source_id, "type": s.source_type,
                                "authority": s.authority, "locator": s.locator} for s in self.sources]
        if self.hints:  # labels and field names only, never the merchant's text
            head["name_checks"] = self.hints

        trusted = [f for f in self.findings if f.source_id not in self.merchant_sources]
        merchant = [f for f in self.findings if f.source_id in self.merchant_sources]
        if trusted:
            head["findings"] = [_finding_view(f) for f in trusted]
        text = json.dumps(head, ensure_ascii=False)
        if merchant:
            text += "\n" + wrap_untrusted(json.dumps([_finding_view(f) for f in merchant], ensure_ascii=False))
        return redact(text)


def _finding_view(f: Finding) -> dict[str, Any]:
    view = {"finding_id": f.finding_id, "source_id": f.source_id, "subject": f.subject,
            "field": f.field, "value": f.value, "confidence": f.confidence}
    if f.note:
        view["note"] = f.note
    return view


def wrap_untrusted(text: str) -> str:
    """Wrap merchant content in tags it can't close early."""
    text = text.replace("<", "\\u003c").replace(">", "\\u003e")
    return f"{UNTRUSTED_OPEN}\n{text}\n{UNTRUSTED_CLOSE}"


class ToolContext:
    """Per-task state shared by a worker's tools: counters, sources, findings, caches."""

    def __init__(self, client: CaseDataClient, task: WorkerInput, now: Optional[datetime] = None):
        self.client = client
        self.task = task
        self.worker: WorkerName = task.worker
        self.now = now or datetime.now()
        self.sources: dict[str, Source] = {}
        self.rendered: dict[str, str] = {}
        self.findings: list[Finding] = []
        self.merchant_sources: set[str] = set()
        self.errors: list[str] = []
        self.tool_calls_used = 0          # kept by the worker loop
        self.tool_path: list[Any] = []    # kyb_contracts.tool_path.ToolCall per requested call; never sent to the Lead
        self.pending_hints: list[str] = []  # name checks of the running call; output_from() takes them
        self._source_by_key: dict[str, str] = {}
        self._outputs: dict[str, ToolOutput] = {}
        self._finding_by_key: dict[tuple, Finding] = {}
        self._next_r = task.record_id_start
        self._next_finding = 1
        self.tin_record_cache: dict[str, Any] = {}

        # Former names learned in this task (or handed over in prior findings),
        # so later name comparisons can try them.
        self.former_names: list[str] = [
            str(f.value) for f in task.prior_findings
            if f.field in {"former_name", "former_names"} and isinstance(f.value, str)
        ]

    # ---- shortcuts ----

    @property
    def case_id(self) -> str:
        return self.task.context.case_id

    @property
    def app(self):
        return self.task.context.application

    @property
    def today(self) -> date:
        return self.now.date()

    # ---- caching of whole calls ----

    def cached(self, key: str, tool: str) -> Optional[ToolOutput]:
        previous = self._outputs.get(key)
        if previous is None:
            return None
        return ToolOutput(tool=tool, sources=previous.sources, findings=previous.findings,
                          note="Already retrieved in this task; same sources and findings as before.",
                          merchant_sources=previous.merchant_sources, cached=True, hints=previous.hints)

    def remember(self, key: str, output: ToolOutput) -> ToolOutput:
        self._outputs[key] = output
        return output

    # ---- sources ----

    def add_source(self, key: str, *, source_type: str, authority: Authority, locator: str,
                   rendered: str, source_id: Optional[str] = None) -> Source:
        """Register a source once per key. Records get the next R ID; case evidence keeps its own ID."""
        existing = self._source_by_key.get(key)
        if existing is not None:
            return self.sources[existing]
        if source_id is None:
            source_id = f"R{self._next_r}"
            self._next_r += 1
        source = Source(source_id=source_id, source_type=source_type, authority=authority,
                        locator=locator, retrieved_at=self.now)
        self.sources[source_id] = source
        self.rendered[source_id] = rendered
        self._source_by_key[key] = source_id
        if authority == Authority.MERCHANT or authority == Authority.MERCHANT.value:
            self.merchant_sources.add(source_id)
        return source

    # ---- findings ----

    def add_finding(self, source: Source, field: str, value: Any, excerpt: str, *,
                    method: ExtractionMethod, confidence: str, subject: str = "business",
                    note: Optional[str] = None) -> Finding:
        if field not in FINDING_FIELDS[self.worker]:
            raise AssertionError(f"the {self.worker} tools may not write the field {field!r}; "
                                 "add it to kyb_contracts.worker_io.FINDING_FIELDS first")
        if excerpt not in self.rendered[source.source_id]:
            raise AssertionError(f"excerpt {excerpt!r} is not in the rendered {source.source_id}")
        key = (source.source_id, field, subject, json.dumps(as_value(value), sort_keys=True, default=str))
        if key in self._finding_by_key:  # the same fact from the same source: report it once
            return self._finding_by_key[key]
        prefix = "rec" if self.worker == "records" else "ev"
        finding = Finding(
            finding_id=f"f_{prefix}_{self.task.task_id}_{self._next_finding}",
            subject=subject, field=field, value=redact_value(field, as_value(value)), source_id=source.source_id,
            extraction_method=method, confidence=confidence,
            citation=Citation(source_id=source.source_id, locator=source.locator, excerpt=excerpt),
            note=redact(note) if note else None,
        )
        self._next_finding += 1
        self.findings.append(finding)
        self._finding_by_key[key] = finding
        return finding

    # ---- name comparisons against the application ----

    def application_names(self) -> list[tuple[str, str, str]]:
        """(against, subject, name) for the applied legal name, DBA, former names and owners."""
        names = [("application.legal_name", "business", self.app.legal_name)]
        if self.app.dba:
            names.append(("application.dba", "business", self.app.dba))
        for former in dict.fromkeys(self.former_names):
            names.append(("former_name", "business", former))
        names += [(owner.ref, owner.ref, owner.name) for owner in self.app.owners]
        return names

    def compare_to_application(self, name: str, *, include_owners: bool = False
                               ) -> list[tuple[str, str, str, NameComparison]]:
        """Compare ``name`` with the application's names.

        Reported, so unrelated pairs don't add noise:
        - always the comparison with the applied legal name,
        - the closest comparison overall (owners count only with ``include_owners`` or
          for a sole proprietor: DBA registrants, account holders, W-9 names),
        - any other comparison that is ``similar`` or closer.
        """
        sole_prop = self.app.entity_type == "SOLE_PROPRIETORSHIP"
        candidates = [(against, subject, other, best_comparison(name, other))
                      for against, subject, other in self.application_names()
                      if not against.startswith("owner:") or include_owners or sole_prop]
        if not candidates:
            return []
        closest = max(candidates, key=lambda c: (LABEL_RANK[c[3].label], c[3].score))
        return [c for c in candidates
                if c[0] == "application.legal_name" or c is closest or c[3].at_least("similar")]

    def name_hints(self, label: str, name: str, *, include_owners: bool = False
                   ) -> list[tuple[str, str, str, NameComparison]]:
        """Compare ``name`` with the application and queue the result as a hint for the model.

        Hints help the worker pick its next lookup (e.g. a mismatch -> read the filing
        history). They are not findings: whether two names are the same business is the
        Lead's call (reconcile.py). A hint names fields and labels only, never the
        compared text, so merchant values can't reach the trusted part of a tool reply.
        """
        comparisons = self.compare_to_application(name, include_owners=include_owners)
        self.pending_hints += [_comparison_note(label, c.label, against)
                               for against, _subject, _other, c in comparisons]
        return comparisons


def _comparison_note(field_label: str, label: str, against: str) -> str:
    meaning = {
        "exact": "identical",
        "normalized_equal": "the same name once case, punctuation and suffix spelling are normalized",
        "core_equal_suffix_differs": "the same name with a different entity suffix",
        "similar": "a look-alike name; NOT evidence of the same entity",
        "different": "a different name",
    }[label]
    return f"{field_label} vs {against}: {label} ({meaning})"


def rank_label(comparisons: list[tuple[str, str, str, NameComparison]]) -> int:
    return max((LABEL_RANK[c.label] for *_, c in comparisons), default=0)


# --------------------------------------------------------------------------
# Tool specs and arguments
# --------------------------------------------------------------------------

class ToolArgs(BaseModel):
    """Base for tool arguments: unknown arguments are an error."""

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[ToolArgs]
    fn: Callable[[ToolContext, Any], ToolOutput]

    def openai_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": schema}}

    def run(self, ctx: ToolContext, raw_args: dict[str, Any]) -> ToolOutput:
        """Validate the arguments and run. Refusals come back as ``rejected``, failures as ``error``."""
        from pydantic import ValidationError

        from app.clients.case_client import CaseServiceError

        try:
            args = self.args_model.model_validate(raw_args or {})
        except ValidationError as error:
            problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}"
                                 for e in error.errors())
            return ToolOutput(tool=self.name, rejected=True, note=f"invalid arguments: {problems}")
        ctx.pending_hints = []
        try:
            return self.fn(ctx, args)
        except ToolInputError as error:
            return ToolOutput(tool=self.name, rejected=True, note=str(error))
        except CaseServiceError as error:
            message = f"{self.name}: case service error: {redact(str(error))}"
            ctx.errors.append(message)
            return ToolOutput(tool=self.name, error=message)
        except Exception as error:  # a bug in a tool must not take the worker down
            message = f"{self.name}: {type(error).__name__}: {redact(str(error))}"
            ctx.errors.append(message)
            return ToolOutput(tool=self.name, error=message)


def output_from(ctx: ToolContext, tool: str, sources: list[Source], findings: list[Finding],
                note: Optional[str] = None) -> ToolOutput:
    unique = list({s.source_id: s for s in sources}.values())
    hints, ctx.pending_hints = list(dict.fromkeys(ctx.pending_hints)), []
    return ToolOutput(tool=tool, sources=unique, findings=findings, note=note,
                      merchant_sources={s.source_id for s in unique} & ctx.merchant_sources, hints=hints)


__all__ = [
    "ID_FIELDS",
    "MAX_RECORDS_PER_CALL",
    "Record",
    "ToolArgs",
    "ToolContext",
    "ToolInputError",
    "ToolOutput",
    "ToolSpec",
    "UNTRUSTED_CLOSE",
    "UNTRUSTED_OPEN",
    "addresses_match",
    "as_value",
    "confidence_from_score",
    "fmt",
    "output_from",
    "parse_date",
    "redact",
    "render_record",
    "wrap_untrusted",
]
