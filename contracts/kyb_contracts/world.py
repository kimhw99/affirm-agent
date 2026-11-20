"""World: world.json, the mocked official and third-party records for one golden case.

No tool in this project calls a real registry, the IRS or a data vendor. The team
writes these records by hand: one world.json per golden case, next to its case.json
(a CaseInput) in data/golden/<ec>/<case_id>/.

The case service loads every world.json into its mock tables and serves them through
read endpoints 1-6 (docs/case-service-endpoints.md). The agent's offline tests read
the same files through a fake client, so both sides see the same data.

This module also defines what endpoints 1-6 return. world.json holds records in
exactly those shapes, with two additions the endpoints don't return:
- a registry entity carries its filing history, which endpoint 3 serves, and
- a commercial profile carries its state, which endpoint 5 filters on.

Rules for this contract, as in case_input.py:
- It checks structure only: types, formats and IDs. Whether a world agrees with its
  case.json (for example with checks.sos_name) belongs in scripts/validate_golden.py.
- No full SSN or EIN anywhere: the TIN record carries the last 4 digits only.
- Every world.json goes into the same tables, so record IDs (state_entity_id,
  event_id, filing_id, profile_id) must be unique across all golden cases, not just
  within one file. This model checks one file; validate_golden.py checks them all.
"""

from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from .case_input import CASE_ID, TIN_PATTERN, Contract, TinType

STATE = r"^[A-Z]{2}$"


# ---------- Fixed values ----------

RegistryEntityType = Literal["LLC", "CORPORATION", "PARTNERSHIP"]  # sole proprietors are never in the registry

EntityStatus = Literal["active", "inactive", "dissolved", "suspended", "pending"]

FilingEventType = Literal[
    "formation",
    "name_change",
    "address_change",
    "officer_change",
    "dissolution",
    "reinstatement",
]

IrsStatus = Literal[
    "issued",      # checks.tin can be Match or Mismatch, depending on the name tested
    "not_issued",  # a brand-new EIN; checks.tin = Not Issued
    "not_found",   # no such number; checks.tin = Not Found
]


def _no_duplicates(kind: str, ids: list[str]) -> None:
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"duplicate {kind}: {duplicates}")


# ---------- What endpoints 1-6 return ----------

class Officer(Contract):
    name: str
    title: str


class RegistryEntity(Contract):
    """Endpoints 1 and 2: one entity in a state's Secretary of State registry."""

    state: str = Field(pattern=STATE)
    state_entity_id: str                          # unique within a state, as in application.state_entity_id
    legal_name: str                               # exactly as the state shows it (checks.sos_name.matched_name)
    former_names: list[str] = Field(default_factory=list)
    entity_type: RegistryEntityType
    entity_status: EntityStatus
    formation_date: date | None = None
    registered_address: str
    officers: list[Officer] = Field(default_factory=list)


class Filing(Contract):
    """Endpoint 3: one event in an entity's filing history."""

    event_id: str
    event_type: FilingEventType
    effective_date: date
    old_value: str | None = None
    new_value: str | None = None


class DbaFiling(Contract):
    """Endpoint 4: one assumed-name (DBA) filing.

    The field names follow the dba_certificate document fields, so a filing and a
    certificate can be compared field for field.
    """

    filing_id: str
    state: str = Field(pattern=STATE)
    county: str | None = None                     # None for a state-level filing
    dba: str
    registrant: str
    registrant_type: Literal["individual", "entity"]
    registrant_state_entity_id: str | None = None  # an entity registrant's state ID, when known
    filed: date
    expires: date | None = None
    status: Literal["active", "expired", "withdrawn"]

    @model_validator(mode="after")
    def only_entities_have_state_ids(self) -> "DbaFiling":
        if self.registrant_type == "individual" and self.registrant_state_entity_id is not None:
            raise ValueError("an individual registrant has no registrant_state_entity_id")
        return self


class CommercialProfile(Contract):
    """Endpoint 5: one profile in the commercial database (LexisNexis / D&B style)."""

    profile_id: str
    business_name: str
    address: str
    first_seen: date
    last_updated: date


class TinRecord(Contract):
    """Endpoint 6: what the IRS has on file for the case's submitted TIN.

    Never the full number. For a sole proprietor filing under an SSN, irs_name is the
    owner's name.
    """

    tin_type: TinType
    tin_last4: str = Field(pattern=r"^\d{4}$")
    irs_status: IrsStatus
    irs_name: str | None = None

    @model_validator(mode="after")
    def name_only_when_issued(self) -> "TinRecord":
        if (self.irs_status == "issued") != (self.irs_name is not None):
            raise ValueError("irs_name is required when irs_status is issued, and must be null otherwise")
        return self


# ---------- world.json ----------

class WorldRegistryEntity(RegistryEntity):
    """A registry entity as world.json holds it: with its filing history, oldest first."""

    filings: list[Filing] = Field(default_factory=list)

    @model_validator(mode="after")
    def filings_oldest_first(self) -> "WorldRegistryEntity":
        dates = [f.effective_date for f in self.filings]
        if dates != sorted(dates):
            raise ValueError(f"{self.state_entity_id}: list the filings oldest first")
        return self


class WorldCommercialProfile(CommercialProfile):
    """A commercial profile as world.json holds it: with the state endpoint 5 filters on."""

    state: str = Field(pattern=STATE)


# Record IDs are skipped by the full-TIN scan, like state_entity_id in CaseInput:
# some states issue 9-digit entity IDs.
_ID_FIELDS = {
    "registry": {"__all__": {"state_entity_id": True, "filings": {"__all__": {"event_id"}}}},
    "dba_filings": {"__all__": {"filing_id", "registrant_state_entity_id"}},
    "commercial": {"__all__": {"profile_id"}},
}


class World(Contract):
    """world.json: every mocked record one golden case needs.

    That is the merchant's own records, plus any others the case is built around,
    such as the unrelated company that really holds an EIN, or look-alike names the
    search has to sort out. A list can be empty: a sole proprietor has no registry
    entity.
    """

    case_id: str = Field(pattern=CASE_ID)         # the golden case this file belongs to
    registry: list[WorldRegistryEntity] = Field(default_factory=list)
    dba_filings: list[DbaFiling] = Field(default_factory=list)
    commercial: list[WorldCommercialProfile] = Field(default_factory=list)
    tin_record: TinRecord | None = None           # None when the case has no submitted TIN (endpoint 6 returns 404)

    @model_validator(mode="after")
    def check_integrity(self) -> "World":
        _no_duplicates("registry entities", [f"{e.state}/{e.state_entity_id}" for e in self.registry])
        _no_duplicates("filing event_id", [f.event_id for e in self.registry for f in e.filings])
        _no_duplicates("DBA filing_id", [d.filing_id for d in self.dba_filings])
        _no_duplicates("commercial profile_id", [c.profile_id for c in self.commercial])

        if TIN_PATTERN.search(self.model_dump_json(exclude=_ID_FIELDS)):
            raise ValueError("a full SSN or EIN appears in the world; keep only the last 4 digits")
        return self
