"""Contract: expected.json, a golden case's expected answer (ExpectedAnswer).

Each golden case folder data/golden/<ec>/<case_id>/ holds case.json (a CaseInput),
world.json (a World) and expected.json: the recommendation the agent should reach,
the evidence its memo must cite, and what it must never do. The evaluation compares
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
from .worker_io import RECORD_ID, RECORDS_FINDING_FIELDS

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
        """Every D, B, W and K ID the answer names, in must_cite (any_of included) and must_not_cite."""
        return _ids(self.must_cite) | _ids(self.must_not_cite)


__all__ = [
    "RECORD_SOURCE_TYPES",
    "AnyOf",
    "Category",
    "EdgeCase",
    "ExpectedAnswer",
    "RecordRef",
    "RecordSourceType",
]
