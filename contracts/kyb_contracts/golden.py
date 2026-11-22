"""Contract: expected.json, a golden case's expected answer (ExpectedAnswer).

Each golden case folder data/golden/<ec>/<case_id>/ holds case.json (a CaseInput),
world.json (a World) and expected.json: the recommendation the agent should reach,
the evidence its memo must cite, what it must never do, and optionally the tool calls
each worker should make (``ToolCalls``, checked by scripts/check_tool_calls.py against the
ToolPaths recorded during a review: tool_path.py; the Lead never sees them). The evaluation compares
each review with it, and data/golden/README.md explains every field. The seed script
never loads it, so the agent can't see the answers.

Rules for this contract, as in the others:
- Structure only: fixed values, ID formats and finding field names. Whether the IDs
  exist in the case's case.json is checked by scripts/validate_golden.py.
- D, B, W and K IDs are fixed when the case arrives, so they're written as they are.
  The Records worker numbers R IDs in the order it calls its tools, so they change from
  run to run: an R record is described by its source type and, if needed, the findings
  it must have (``RecordRef``).
- No full SSN or EIN in the free text.
"""

import re
from typing import Annotated, Any, Literal, Union, get_args

from pydantic import AfterValidator, Discriminator, Field, Tag, field_validator, model_validator

from .case_input import CASE_ID, TIN_PATTERN, Contract
from .memo import Recommendation
from .worker_io import FINDING_FIELDS, RECORD_ID, RECORDS_FINDING_FIELDS

FIXED_ID = r"^[DBWK]\d+$"                    # case_input.py's DOC_ID, BANK_ID, WEB_ID and KYC_ID


# ---------- Fixed values ----------

Category = Literal[
    "normal",             # a routine case the agent should settle, such as formatting differences only
    "business_scenario",  # an important business situation, such as a rename or a DBA filed by the owner
    "exception",          # missing, conflicting or misleading data the agent has to sort out
    "invalid_input",      # invalid or incomplete inputs, such as no TIN or no documents
    "unsafe",             # an unsafe or unauthorized request, such as instructions hidden in merchant text
    "hand_to_human",      # the agent should stay unsure and leave the case to a person
]

EdgeCase = Literal["EC-1", "EC-2", "EC-4"]

# The source types the Records worker's tools give their R sources
# (services/agent/app/tools/registry.py and tin_kyc.py).
RecordSourceType = Literal[
    "sos_registry",           # get_registry_entity, or an entity found by search_registry
    "sos_registry_search",    # search_registry, when it finds nothing
    "sos_filing_history",     # get_filing_history
    "county_dba",             # search_dba: a county filing
    "state_dba",              # search_dba: a state-level filing
    "dba_search",             # search_dba, when it finds nothing
    "commercial_db",          # search_commercial_db: one profile
    "commercial_db_search",   # search_commercial_db, when it finds nothing
    "irs_tin_match",          # check_tin_match
]
RECORD_SOURCE_TYPES: frozenset[str] = frozenset(get_args(RecordSourceType))


# ---------- What the memo must cite ----------

def _fixed_id(value: str) -> str:
    # The value stays out of the messages, as in validate_golden.py, in case it's a full TIN.
    if re.match(RECORD_ID, value):
        raise ValueError("R IDs change from run to run, so describe the record by its source")
    if not re.match(FIXED_ID, value):
        raise ValueError("not a D, B, W or K evidence ID")
    return value


FixedId = Annotated[str, AfterValidator(_fixed_id)]   # D1, B1, W1, K1


class RecordRef(Contract):
    """An R record, by what it is: its source type, and the findings it must have.

    ``where`` maps finding fields to values, as the Records worker's tools write them
    (``RECORDS_FINDING_FIELDS`` in worker_io.py). A cited R record matches when its source
    type is ``source`` and, for each field, one of its findings has that value.
    """

    source: RecordSourceType
    where: dict[str, Any] = Field(default_factory=dict)

    @field_validator("where")
    @classmethod
    def known_fields(cls, where: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(where) - RECORDS_FINDING_FIELDS)
        if unknown:
            raise ValueError(f"no Records tool writes the finding field(s) {unknown} "
                             "(RECORDS_FINDING_FIELDS in worker_io.py)")
        return where


def _form(value: Any) -> str:
    """Which form a citation takes: an ID (a string), any_of, or an R record."""
    if isinstance(value, str):
        return "id"
    if isinstance(value, dict):
        return "any_of" if "any_of" in value else "record"
    return "any_of" if hasattr(value, "any_of") else "record"


# One item to cite, or not to cite: a D, B, W or K ID, or an R record.
Cited = Annotated[
    Union[Annotated[FixedId, Tag("id")], Annotated[RecordRef, Tag("record")]],
    Discriminator(_form, custom_error_type="any_of_here",
                  custom_error_message="any_of can't go inside another any_of, or in must_not_cite"),
]


class AnyOf(Contract):
    """Citing at least one of these counts."""

    any_of: list[Cited] = Field(min_length=2)


# One item of must_cite: an ID, an R record, or any_of.
MustCite = Annotated[
    Union[Annotated[FixedId, Tag("id")], Annotated[RecordRef, Tag("record")], Annotated[AnyOf, Tag("any_of")]],
    Discriminator(_form),
]


# ---------- The tool calls each worker should make ----------

# The tools of each worker (services/agent/app/tools/__init__.py, TOOLS_FOR); an agent
# test keeps these lists equal to the real ones.
RecordsTool = Literal["search_registry", "get_registry_entity", "get_filing_history", "search_dba",
                      "search_commercial_db", "check_tin_match", "get_kyc_result"]
EvidenceTool = Literal["get_document", "get_bank_verification", "get_site_identity"]
WORKER_TOOLS: dict[str, frozenset[str]] = {
    "records": frozenset(get_args(RecordsTool)),
    "evidence": frozenset(get_args(EvidenceTool)),
}

# The sources each tool returns: an R source type (RecordSourceType), or for the tools that
# read the case's own evidence, the prefix of its fixed ID (get_document returns D1 itself).
TOOL_SOURCES: dict[str, frozenset[str]] = {
    "search_registry": frozenset({"sos_registry", "sos_registry_search"}),
    "get_registry_entity": frozenset({"sos_registry"}),
    "get_filing_history": frozenset({"sos_filing_history"}),
    "search_dba": frozenset({"county_dba", "state_dba", "dba_search"}),
    "search_commercial_db": frozenset({"commercial_db", "commercial_db_search"}),
    "check_tin_match": frozenset({"irs_tin_match"}),
}
TOOL_ID_PREFIX: dict[str, str] = {
    "get_kyc_result": "K",
    "get_document": "D",
    "get_bank_verification": "B",
    "get_site_identity": "W",
}


class ToolCallRef(Contract):
    """One tool call, by the tool and what it returned.

    ``source`` is an R source type for the Records tools that look records up (their R IDs
    change from run to run), or the fixed ID for the tools that read the case's own evidence
    (D1, B1, W1, K1). ``where`` maps finding fields to values, as in ``RecordRef``. A call
    matches when its tool is ``tool``, it ran (a repeat counts), and one of its sources is
    ``source`` and has a finding with each value in ``where``.
    """

    tool: str
    source: str | None = None
    where: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def source_fits_tool(self) -> "ToolCallRef":
        if self.tool not in TOOL_SOURCES and self.tool not in TOOL_ID_PREFIX:
            raise ValueError(f"{self.tool} is not a worker tool")
        if self.source is None:
            return self
        if self.tool in TOOL_ID_PREFIX:
            if re.match(RECORD_ID, self.source) or not re.match(FIXED_ID, self.source) \
                    or self.source[0] != TOOL_ID_PREFIX[self.tool]:
                raise ValueError(f"{self.tool} returns the case's {TOOL_ID_PREFIX[self.tool]} evidence, "
                                 f"so its source is an ID such as {TOOL_ID_PREFIX[self.tool]}1")
        elif self.source not in TOOL_SOURCES[self.tool]:
            raise ValueError(f"{self.tool} returns sources of type {sorted(TOOL_SOURCES[self.tool])}")
        return self

    def describe(self) -> str:
        """For reports: check_tin_match -> irs_tin_match where name_control_tested=HARB."""
        text = self.tool + (f" -> {self.source}" if self.source else "")
        if self.where:
            text += " where " + ", ".join(f"{k}={v}" for k, v in self.where.items())
        return text


class WorkerToolCalls(Contract):
    """What one worker should do over the whole review: its first task and every follow-up."""

    must_call: list[ToolCallRef] = Field(default_factory=list)       # each one matched by some call
    must_not_call: list[ToolCallRef] = Field(default_factory=list)   # matched by no call, e.g. a look-alike
    max_calls: int | None = Field(default=None, ge=1)    # calls counted in tool_calls_used, all tasks together
    max_repeats: int | None = Field(default=None, ge=0)  # calls that fetched again what an earlier one had


class ToolCalls(Contract):
    """expected.json's tool_calls: per worker, the tool path the evaluation checks."""

    records: WorkerToolCalls | None = None
    evidence: WorkerToolCalls | None = None

    @model_validator(mode="after")
    def tools_belong_to_worker(self) -> "ToolCalls":
        for worker in ("records", "evidence"):
            calls = getattr(self, worker)
            if calls is None:
                continue
            for ref in [*calls.must_call, *calls.must_not_call]:
                if ref.tool not in WORKER_TOOLS[worker]:
                    raise ValueError(f"{ref.tool} is not one of the {worker} worker's tools "
                                     f"({sorted(WORKER_TOOLS[worker])})")
                unknown = sorted(set(ref.where) - FINDING_FIELDS[worker])
                if unknown:
                    raise ValueError(f"no {worker} tool writes the finding field(s) {unknown} "
                                     "(FINDING_FIELDS in worker_io.py)")
        return self

    def fixed_ids(self) -> set[str]:
        """The D, B, W and K IDs named as sources."""
        refs = [ref for calls in (self.records, self.evidence) if calls
                for ref in (*calls.must_call, *calls.must_not_call)]
        return {ref.source for ref in refs if ref.source and ref.tool in TOOL_ID_PREFIX}


def _ids(items: list[Any]) -> set[str]:
    """The D, B, W and K IDs among some citations, inside any_of too."""
    ids: set[str] = set()
    for item in items:
        if isinstance(item, str):
            ids.add(item)
        elif isinstance(item, AnyOf):
            ids |= _ids(item.any_of)
    return ids


# ---------- expected.json ----------

class ExpectedAnswer(Contract):
    """expected.json: what the agent should conclude for one golden case, and how to tell."""

    case_id: str = Field(pattern=CASE_ID)                  # the folder's case_id, as in case.json
    categories: list[Category] = Field(min_length=1)       # what kind of case it is, for the coverage report
    edge_cases: list[EdgeCase] = Field(min_length=1)
    expected_decision: Recommendation                      # the memo's recommendation
    acceptable_decisions: list[Recommendation] = Field(default_factory=list)  # other ones that also count as right
    must_cite: list[MustCite]                              # the memo cites every item (any_of: at least one of it)
    must_not_cite: list[Cited] = Field(default_factory=list)
    forbidden_actions: list[Annotated[str, Field(min_length=1)]]   # in words, for the evaluation or a reviewer
    max_follow_up_rounds: int | None = Field(default=None, ge=0)   # the workflow itself stops after 2
    tool_calls: ToolCalls | None = None                    # the tool path per worker, if the case checks it
    notes: str = Field(min_length=1)                       # why this is the answer, for people

    @model_validator(mode="after")
    def check_integrity(self) -> "ExpectedAnswer":
        for name in ("categories", "edge_cases", "acceptable_decisions", "forbidden_actions"):
            values = getattr(self, name)
            duplicates = sorted({v for v in values if values.count(v) > 1})
            if duplicates:
                raise ValueError(f"{name} lists {duplicates} more than once")
        if self.expected_decision in self.acceptable_decisions:
            raise ValueError("acceptable_decisions repeats expected_decision; list only the other decisions")
        both = sorted(_ids(self.must_cite) & _ids(self.must_not_cite))
        if both:
            raise ValueError(f"{both} is in both must_cite and must_not_cite")
        if TIN_PATTERN.search(" ".join([self.notes, *self.forbidden_actions])):
            raise ValueError("a full SSN or EIN appears in the text; keep only the last 4 digits")
        return self

    def decisions(self) -> list[str]:
        """Every recommendation that counts as right: expected_decision first."""
        return [self.expected_decision, *self.acceptable_decisions]

    def fixed_ids(self) -> set[str]:
        """Every D, B, W and K ID the answer names: in must_cite (any_of included), must_not_cite
        and the tool calls' sources."""
        ids = _ids(self.must_cite) | _ids(self.must_not_cite)
        return ids | self.tool_calls.fixed_ids() if self.tool_calls else ids


__all__ = [
    "RECORD_SOURCE_TYPES",
    "TOOL_ID_PREFIX",
    "TOOL_SOURCES",
    "WORKER_TOOLS",
    "AnyOf",
    "Category",
    "EdgeCase",
    "EvidenceTool",
    "ExpectedAnswer",
    "RecordRef",
    "RecordSourceType",
    "RecordsTool",
    "ToolCallRef",
    "ToolCalls",
    "WorkerToolCalls",
]
