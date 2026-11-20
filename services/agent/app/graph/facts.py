"""A read-only index over the workers' findings, used by reconcile and the guardrails.

Workers report findings with a ``field`` name (``worker_io.Finding.field``). Each
logical fact below lists the field names it accepts: first the names the BEAC-7
workers' tools write (``app/tools``), then older spellings kept for compatibility.
Evidence is also told apart by *where* it came from (the source kind: an R record's
``source_type``, or the case document type for a D ID).

What the BEAC-7 Records worker writes, and how the Lead reads it
    get_registry_entity / search_registry -> legal_name, entity_status, state_entity_id,
        formation_date, former_names (a list), officer_name; one R source per entity.
        A search can return look-alike entities, so registry facts are split by entity:
        the applicant's entity (its state_entity_id, or the closest name) feeds the
        registry checks; any other entity is a "separate entity", never the applicant.
        Nothing found -> registry_match "none" / registry_entity "not_found".
    get_filing_history -> filing_event, former_name (one per name change)
    search_dba -> dba, registrant, registrant_state_entity_id, dba_status, dba_expired
        (an expired or withdrawn filing no longer ties the DBA to anyone)
    check_tin_match -> tin_match_result ("Match" | "Mismatch" | "Not Issued" |
        "Not Found"); the name tested is in the R source's locator ("... name=<name>")
        and in the name_control_tested finding's note. name_control_tested is the
        control of the *tested* name, not the IRS's, so it isn't irs_name_control.
    get_kyc_result -> kyc_result (subject owner:Pn)

What the BEAC-7 Evidence worker writes
    get_document -> each extracted field under its own name (name, legal_name,
        business_name, holder, dba, registrant, ein_last4, name_control, date, status,
        formation_date, ...), plus derived checks and injection_suspected
    get_bank_verification -> account_holder (the Lead splits "LEGAL DBA TRADE" itself), bank_status
    get_site_identity -> website_legal_name, website_dba, website_address,
        injection_suspected (true or false)

Name matching is the Lead's: reconcile.py compares records against evidence with
app/shared/name_norm.py. The tools only show the worker's model name_checks hints
(which lookup next); they never write name-comparison findings.

Only facts some tool can report are listed here. There is no pending-filing,
domain-age or tampering fact: the case service and world.json carry no such data. A
filed-but-unapproved formation shows up as the registry's entity_status "pending".

A W-9's business_name is line 2 ("business name / disregarded entity name"), i.e. a
trade name, so it counts as a DBA, not a legal name.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Optional

from app.shared.name_norm import LABEL_RANK, best_comparison
from kyb_contracts.case_input import CaseInput
from kyb_contracts.worker_io import WorkerResult

# ---------- Logical facts -> accepted field names ----------

RECORDS_FIELDS: dict[str, frozenset[str]] = {
    "legal_name": frozenset({"legal_name", "entity_name", "registry_legal_name"}),
    "entity_status": frozenset({"entity_status", "status"}),
    "entity_id": frozenset({"state_entity_id", "entity_id"}),
    "formation_date": frozenset({"formation_date", "formed"}),
    "former_name": frozenset({"former_name", "former_names", "name_change", "name_history"}),
    "dba_name": frozenset({"dba_name", "dba"}),
    "dba_owner": frozenset({"dba_owner", "dba_registrant", "registrant"}),
    "tin_match": frozenset({"tin_match", "tin_match_result"}),
    "name_control": frozenset({"irs_name_control", "name_control"}),
    "kyc_result": frozenset({"kyc_result", "kyc"}),
    "registry_found": frozenset({"registry_match", "registry_entity", "registry_found", "entity_found"}),
    "separate_entity": frozenset({"separate_entity", "ein_owner_entity", "unrelated_entity"}),
    "name_tested": frozenset({"name_control_tested"}),
    "dba_owner_entity_id": frozenset({"registrant_state_entity_id"}),
    "dba_status": frozenset({"dba_status"}),          # world.DbaFiling.status: active | expired | withdrawn
    "dba_expired": frozenset({"dba_expired"}),        # derived by search_dba: expired, or past its expiry date
}

# Facts that describe one registry entity. A search can return several entities; only
# the applicant's feed these facts, and other entities' legal names become separate_entity.
ENTITY_FACTS = frozenset({"legal_name", "entity_status", "entity_id", "formation_date", "former_name"})
_REGISTRY_ENTITY_TYPES = frozenset({"sos_registry"})

EVIDENCE_FIELDS: dict[str, frozenset[str]] = {
    "name": frozenset({"name", "legal_name", "holder", "website_legal_name", "new_name"}),
    "dba": frozenset({"dba", "dba_name", "website_dba", "business_name"}),
    "registrant": frozenset({"registrant", "dba_owner"}),
    "ein_last4": frozenset({"ein_last4", "tin_last4"}),
    "name_control": frozenset({"name_control", "ein_letter_name_control"}),
    "date": frozenset({"date", "issue_date", "issued", "filed", "ein_letter_date"}),
    "formation_date": frozenset({"formation_date"}),
    "account_holder": frozenset({"account_holder", "bank_account_holder"}),
    "bank_status": frozenset({"bank_status", "status", "verification_status"}),
    "license_status": frozenset({"license_status", "status"}),
    "tin_type": frozenset({"tin_type"}),
    "suspicious": frozenset({"injection_suspected", "suspicious_content", "instruction_like_text", "suspicious"}),
    "old_name": frozenset({"old_name"}),
}

EIN_LETTERS = frozenset({"irs_cp575", "irs_147c"})
FORMATION_DOCS = frozenset({"articles", "good_standing"})
NAME_DOCS = frozenset({"business_license", "articles", "good_standing", "w9", "articles_of_amendment"})

# Trust ranking: official records, then third-party documents, then the merchant's own claims.
AUTHORITY_RANK = {"official": 2, "third_party": 1, "merchant": 0}
_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class Fact:
    key: str              # globally unique: "<task_id>:<finding_id>"
    finding_id: str
    task_id: str
    worker: str           # "records" | "evidence"
    subject: str          # "business" | "owner:P1"
    field: str
    value: Any
    source_id: str        # evidence ID: R/K/D/B/W
    source_kind: str      # R: source_type; D: document type; B: "bank"; W: "website"; K: "owner_kyc"
    authority: str        # official | third_party | merchant
    confidence: str
    excerpt: str
    note: Optional[str]
    round: int            # 0 = first pass; n = follow-up round n


def _source_kind(source_id: str, source_type: str, doc_types: dict[str, str]) -> str:
    prefix = source_id[0]
    if prefix == "D":
        return doc_types.get(source_id, source_type)
    if prefix == "B":
        return "bank"
    if prefix == "W":
        return "website"
    if prefix == "K":
        return "owner_kyc"
    return source_type


class FactIndex:
    """All findings from every worker result so far, newest last."""

    def __init__(self, case: CaseInput, results: Iterable[tuple[int, WorkerResult]]):
        self.case = case
        self.doc_types = {d.id: d.type for d in case.documents}
        self.facts: list[Fact] = []
        self.source_types: dict[str, str] = {}
        self.sources: dict[str, Any] = {}
        for round_no, result in results:
            src = {s.source_id: s for s in result.sources}
            self.sources.update(src)
            for s in result.sources:
                self.source_types[s.source_id] = s.source_type
            for f in result.findings:
                s = src.get(f.source_id)
                self.facts.append(Fact(
                    key=f"{result.task_id}:{f.finding_id}",
                    finding_id=f.finding_id,
                    task_id=result.task_id,
                    worker=result.worker,
                    subject=f.subject,
                    field=f.field,
                    value=f.value,
                    source_id=f.source_id,
                    source_kind=_source_kind(f.source_id, s.source_type if s else "", self.doc_types),
                    authority=(s.authority if s else _default_authority(f.source_id)),
                    confidence=f.confidence,
                    excerpt=f.citation.excerpt,
                    note=f.note,
                    round=round_no,
                ))
        self.applicant_entities, self.other_entities = self._split_entities()

    # ----- registry entities -----

    def _split_entities(self) -> tuple[set[str], set[str]]:
        """R sources that are registry entities: the applicant's, and everyone else's."""
        entities: dict[str, dict[str, Fact]] = {}
        for f in self.facts:
            if f.worker == "records" and f.source_id.startswith("R") \
                    and self.source_types.get(f.source_id) in _REGISTRY_ENTITY_TYPES \
                    and f.field in ("legal_name", "state_entity_id", "former_names"):
                entities.setdefault(f.source_id, {})[f.field] = f
        if not entities:
            return set(), set()
        app = self.case.application
        wanted_id = (app.state_entity_id or "").strip().upper()
        if wanted_id:
            by_id = {sid for sid, d in entities.items()
                     if "state_entity_id" in d and str(d["state_entity_id"].value).strip().upper() == wanted_id}
            if by_id:
                return by_id, set(entities) - by_id
        # No entity number (or none found by it): the entity whose name is the applied one.
        names = [n for n in (app.legal_name, app.dba) if n]

        def rank(d: dict[str, Fact]) -> int:
            cands = [str(d["legal_name"].value)] if "legal_name" in d else []
            former = d.get("former_names")
            if former is not None:
                cands += [str(x) for x in (former.value if isinstance(former.value, list) else [former.value])]
            return max((LABEL_RANK[best_comparison(c, n).label] for c in cands for n in names), default=0)

        close = {sid for sid, d in entities.items() if rank(d) >= LABEL_RANK["core_equal_suffix_differs"]}
        return close, set(entities) - close

    def _entity_ok(self, f: Fact) -> bool:
        return f.source_id not in self.other_entities

    # ----- queries -----

    def records(self, fact: str, *, subject: Optional[str] = None,
                kinds: Optional[Iterable[str]] = None) -> list[Fact]:
        found = self._find("records", RECORDS_FIELDS[fact], subject, kinds)
        if fact in ENTITY_FACTS:
            found = [f for f in found if self._entity_ok(f)]
        if fact == "separate_entity":  # look-alike or unrelated companies the registry returned
            found += [f for f in self._find("records", RECORDS_FIELDS["legal_name"], subject, kinds)
                      if f.source_id in self.other_entities]
        return found

    def evidence(self, fact: str, *, subject: Optional[str] = None,
                 kinds: Optional[Iterable[str]] = None) -> list[Fact]:
        return self._find("evidence", EVIDENCE_FIELDS[fact], subject, kinds)

    def _find(self, worker: str, fields: frozenset[str], subject, kinds) -> list[Fact]:
        kinds = set(kinds) if kinds is not None else None
        out = []
        for f in self.facts:
            if f.worker != worker or f.field not in fields:
                continue
            if subject is not None and f.subject != subject:
                continue
            if kinds is not None and f.source_kind not in kinds:
                continue
            out.append(f)
        return out

    def by_key(self, key: str) -> Optional[Fact]:
        for f in self.facts:
            if f.key == key or f.finding_id == key:
                return f
        return None

    def known_evidence_ids(self) -> set[str]:
        ids = set(self.sources)
        ids |= {d.id for d in self.case.documents}
        ids |= {w.id for w in self.case.web_snapshot}
        ids |= {k.id for k in self.case.owner_kyc}
        if self.case.bank_verification:
            ids.add(self.case.bank_verification.id)
        return ids

    def max_record_number(self) -> int:
        nums = [int(sid[1:]) for sid in self.sources if sid.startswith("R")]
        return max(nums, default=0)

    def finding_keys_in_round(self, round_no: int) -> set[str]:
        return {f.key for f in self.facts if f.round == round_no}


def _default_authority(source_id: str) -> str:
    return {"R": "official", "K": "third_party", "B": "third_party"}.get(source_id[0], "merchant")


def weakest_conf(facts: Iterable[Fact], default: str = "low") -> str:
    levels = [f.confidence for f in facts]
    if not levels:
        return default
    return min(levels, key=_CONF_RANK.__getitem__)


def cap_conf(level: str, cap: str) -> str:
    return level if _CONF_RANK[level] <= _CONF_RANK[cap] else cap


def text(value: Any) -> Optional[str]:
    """A fact value as a string, or None for empty values."""
    if value is None:
        return None
    if isinstance(value, dict):
        for k in ("name", "new_name", "value", "result"):
            if value.get(k):
                return str(value[k])
        return None
    s = str(value).strip()
    return s or None


def as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "y"}
    return bool(value)


def ids(facts: Iterable[Fact]) -> list[str]:
    """Evidence IDs of the given facts, de-duplicated, in first-seen order."""
    seen: dict[str, None] = {}
    for f in facts:
        seen.setdefault(f.source_id, None)
    return list(seen)


__all__ = [
    "AUTHORITY_RANK",
    "EIN_LETTERS",
    "EVIDENCE_FIELDS",
    "FORMATION_DOCS",
    "Fact",
    "FactIndex",
    "NAME_DOCS",
    "RECORDS_FIELDS",
    "as_date",
    "cap_conf",
    "ids",
    "text",
    "truthy",
    "weakest_conf",
]
