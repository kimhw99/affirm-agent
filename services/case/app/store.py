"""Reads from case_db, returned as the kyb_contracts models the endpoints serve, and
the one write: saving the analyst's decision (step 11).

Every function takes an open connection whose rows come back as dicts (psycopg's
dict_row). A lookup returns None when nothing matches; a search returns a list,
possibly empty. The SQL selects only the columns a contract has, so source_case,
raw_text and page_text never leave the database.

Searches work like FakeCaseClient in services/agent/app/clients/case_client.py, so the
agent's offline tests and the real service find the same records. A field matches
when any significant word of the query appears in it, ignoring case. Every criterion
given must match, and at most 20 rows come back, best matches first. Unlike the fake,
there is no fuzzy matching of misspelled words yet.
"""

import json
import re
from datetime import datetime, timezone

from kyb_contracts.case_input import BankVerification, CaseInput, Document, OwnerKYC, WebSnapshot
from kyb_contracts.decision import STATUS_AFTER, DecisionRecord
from kyb_contracts.world import CommercialProfile, DbaFiling, Filing, RegistryEntity, TinRecord

SEARCH_LIMIT = 20

# Words too common to search on (entity designators and joiners), as in FakeCaseClient.
STOPWORDS = {
    "the", "and", "of", "llc", "lc", "pllc", "inc", "incorporated", "corp", "corporation",
    "co", "company", "ltd", "limited", "llp", "lp", "pc", "dba",
}

APPLICATION = ("legal_name", "dba", "address", "tin_type", "tin_last4", "state", "state_entity_id",
               "entity_type", "formation_date")
ENTITY = ("state, state_entity_id, legal_name, former_names, entity_type, entity_status, "
          "formation_date, registered_address, officers")
DBA = ("filing_id, state, county, dba, registrant, registrant_type, registrant_state_entity_id, "
       "filed, expires, status")
COMMERCIAL = "profile_id, business_name, address, first_seen, last_updated"

# A case's evidence, with the column names of the contract.
OWNERS = "SELECT owner_ref AS ref, name, role FROM case_owners WHERE case_id = %s"
DOCUMENTS = "SELECT doc_id AS id, type, extracted, confidence FROM documents WHERE case_id = %s"
BANK = "SELECT bank_id AS id, account_holder, status, verified_at FROM bank_verifications WHERE case_id = %s"
WEB = "SELECT web_id AS id, url, captured_at, extracted FROM web_snapshots WHERE case_id = %s"
KYC = "SELECT kyc_id AS id, owner_ref, result FROM owner_kyc WHERE case_id = %s"

# A county for comparing: "Cook County" and "cook" are the same, as in FakeCaseClient.
COUNTY = r"lower(regexp_replace(trim({}), '\s+county$', '', 'i'))"


def search_words(text: str | None) -> list[str]:
    """The words of a query worth matching, picked as FakeCaseClient picks them."""
    words = re.sub(r"[^0-9a-z]+", " ", (text or "").casefold().replace("&", " and ")).split()
    significant = [w for w in words if w not in STOPWORDS and len(w) > 1]
    return list(dict.fromkeys(significant or words))


def _rows(conn, sql: str, *params) -> list[dict]:
    return conn.execute(sql, params).fetchall()


def _one(conn, model, sql: str, *params):
    row = conn.execute(sql, params).fetchone()
    return None if row is None else model.model_validate(row)


def _search(conn, table: str, columns: str, where: str, where_params: list, order: str,
            criteria: list[tuple[str, str | None]]) -> list[dict]:
    """The rows of table that pass where and match every criterion given, best first.

    A criterion is a condition on one word, whose %s placeholders all get that word's
    ILIKE pattern, and the query text. A criterion without text is skipped. A row's
    score is how many query words it matches.
    """
    counts, params = [], []
    for condition, text in criteria:
        if not text:
            continue
        words = search_words(text)
        if not words:
            return []  # nothing in the text to match on
        counts.append(" + ".join(f"({condition})::int" for _ in words))
        params += [f"%{word}%" for word in words for _ in range(condition.count("%s"))]
    if not counts:
        return []
    hits = [f"hits{i}" for i in range(len(counts))]
    sql = (f"SELECT {columns} FROM ("
           f"SELECT *, {', '.join(f'{count} AS {hit}' for count, hit in zip(counts, hits))} "
           f"FROM {table} WHERE {where}) AS candidates "
           f"WHERE {' AND '.join(f'{hit} > 0' for hit in hits)} "
           f"ORDER BY {' + '.join(hits)} DESC, {order} LIMIT {SEARCH_LIMIT}")
    return conn.execute(sql, params + where_params).fetchall()


# ---------- GET /cases/{case_id} ----------

def get_case(conn, case_id: str) -> CaseInput | None:
    case = conn.execute(f"SELECT case_id, status, flag_reasons, checks, {', '.join(APPLICATION)} "
                        "FROM cases WHERE case_id = %s", [case_id]).fetchone()
    if case is None:
        return None
    application = {field: case.pop(field) for field in APPLICATION}
    # Sorted by length first, so D2 comes before D10.
    application["owners"] = _rows(conn, OWNERS + " ORDER BY length(owner_ref), owner_ref", case_id)
    bank = _rows(conn, BANK, case_id)
    return CaseInput.model_validate(case | {
        "application": application,
        "documents": _rows(conn, DOCUMENTS + " ORDER BY length(doc_id), doc_id", case_id),
        "bank_verification": bank[0] if bank else None,
        "web_snapshot": _rows(conn, WEB + " ORDER BY length(web_id), web_id", case_id),
        "owner_kyc": _rows(conn, KYC + " ORDER BY length(kyc_id), kyc_id", case_id),
    })


# ---------- Records worker: official and third-party data (endpoints 1-7) ----------

def search_registry(conn, state: str, name: str | None = None, officer: str | None = None,
                    address: str | None = None) -> list[RegistryEntity]:
    """Endpoint 1. A name matches the legal name or any former name."""
    rows = _search(conn, "mock.registry_entities", ENTITY, "state = upper(%s)", [state], "legal_name", [
        ("legal_name ILIKE %s OR EXISTS (SELECT 1 FROM unnest(former_names) AS f(n) WHERE f.n ILIKE %s)", name),
        ("EXISTS (SELECT 1 FROM jsonb_array_elements(officers) AS o(v) WHERE o.v->>'name' ILIKE %s)", officer),
        ("registered_address ILIKE %s", address),
    ])
    return [RegistryEntity.model_validate(row) for row in rows]


def get_registry_entity(conn, state: str, state_entity_id: str) -> RegistryEntity | None:
    """Endpoint 2."""
    return _one(conn, RegistryEntity, f"SELECT {ENTITY} FROM mock.registry_entities "
                "WHERE state = upper(%s) AND state_entity_id = %s", state, state_entity_id)


def get_filings(conn, state: str, state_entity_id: str) -> list[Filing] | None:
    """Endpoint 3: the entity's filing history, oldest first, or None if there's no such entity."""
    if get_registry_entity(conn, state, state_entity_id) is None:
        return None
    rows = _rows(conn, "SELECT event_id, event_type, effective_date, old_value, new_value "
                       "FROM mock.registry_filings WHERE state = upper(%s) AND state_entity_id = %s "
                       "ORDER BY effective_date, event_id", state, state_entity_id)
    return [Filing.model_validate(row) for row in rows]


def search_dba(conn, state: str, name: str, county: str | None = None) -> list[DbaFiling]:
    """Endpoint 4. Without a county it searches state-level filings and every county."""
    where, params = "state = upper(%s)", [state]
    if county:
        where += f" AND {COUNTY.format('county')} = {COUNTY.format('%s')}"
        params.append(county)
    rows = _search(conn, "mock.dba_filings", DBA, where, params, "dba", [("dba ILIKE %s", name)])
    return [DbaFiling.model_validate(row) for row in rows]


def search_commercial(conn, state: str, name: str | None = None,
                      address: str | None = None) -> list[CommercialProfile]:
    """Endpoint 5. The state is a filter only; profiles come back without it."""
    rows = _search(conn, "mock.commercial_profiles", COMMERCIAL, "state = upper(%s)", [state], "business_name",
                   [("business_name ILIKE %s", name), ("address ILIKE %s", address)])
    return [CommercialProfile.model_validate(row) for row in rows]


def get_tin_record(conn, case_id: str) -> TinRecord | None:
    """Endpoint 6: None when the case has no submitted TIN, or doesn't exist."""
    return _one(conn, TinRecord, "SELECT tin_type, tin_last4, irs_status, irs_name "
                "FROM mock.tin_records WHERE case_id = %s", case_id)


def get_owner_kyc(conn, case_id: str, kyc_id: str) -> OwnerKYC | None:
    """Endpoint 7."""
    return _one(conn, OwnerKYC, KYC + " AND kyc_id = %s", case_id, kyc_id)


# ---------- Evidence worker: merchant evidence (endpoints 8-10) ----------

def get_document(conn, case_id: str, doc_id: str) -> Document | None:
    """Endpoint 8, without the OCR text."""
    return _one(conn, Document, DOCUMENTS + " AND doc_id = %s", case_id, doc_id)


def get_bank_verification(conn, case_id: str, bank_id: str) -> BankVerification | None:
    """Endpoint 9."""
    return _one(conn, BankVerification, BANK + " AND bank_id = %s", case_id, bank_id)


def get_web_snapshot(conn, case_id: str, web_id: str) -> WebSnapshot | None:
    """Endpoint 10, without the page text."""
    return _one(conn, WebSnapshot, WEB + " AND web_id = %s", case_id, web_id)


# ---------- Step 11: the analyst's decision ----------

DECISION = "review_id, case_id, decision, note, analyst_id, agent_recommendation, overridden, decided_at"

# The statuses in which a case can take a decision. Approved and denied cases are done,
# and a docs_requested case waits for the merchant's new evidence (CaseStatus).
DECIDABLE = {"pending_review", "escalated"}


class CaseNotFound(LookupError):
    """There's no case with this ID."""


class DecisionConflict(Exception):
    """The review already has a different decision, or the case can't take one now."""


def _utc(moment: datetime) -> datetime:
    """A time without a time zone counts as UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def same_decision(a: DecisionRecord, b: DecisionRecord) -> bool:
    """Whether two records are the same decision, at the same moment."""
    return (a.model_dump(exclude={"decided_at"}) == b.model_dump(exclude={"decided_at"})
            and _utc(a.decided_at) == _utc(b.decided_at))


def save_decision(conn, record: DecisionRecord) -> bool:
    """Step 11: saves the decision, the case's new status and an audit log entry, all or nothing.

    Returns False, changing nothing, when this review's decision was already saved with
    the same content, so a retry is safe. Raises CaseNotFound or DecisionConflict.
    """
    to_status = STATUS_AFTER[record.decision].value
    with conn.transaction():
        # Locks the case's row, so two decisions for one case can't both pass the checks.
        case = conn.execute("SELECT status FROM cases WHERE case_id = %s FOR UPDATE", [record.case_id]).fetchone()
        if case is None:
            raise CaseNotFound(record.case_id)
        saved = conn.execute(f"SELECT {DECISION} FROM decisions WHERE review_id = %s", [record.review_id]).fetchone()
        if saved is not None:
            if same_decision(DecisionRecord.model_validate(saved), record):
                return False
            raise DecisionConflict(f"review {record.review_id} already has a different decision")
        if case["status"] not in DECIDABLE:
            raise DecisionConflict(f"case {record.case_id} is {case['status']}, so it can't take a decision")
        conn.execute(f"INSERT INTO decisions ({DECISION}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                     [record.review_id, record.case_id, record.decision, record.note, record.analyst_id,
                      record.agent_recommendation, record.overridden, _utc(record.decided_at)])
        conn.execute("UPDATE cases SET status = %s, updated_at = now() WHERE case_id = %s",
                     [to_status, record.case_id])
        conn.execute("INSERT INTO audit_log (case_id, review_id, actor_id, action, from_status, to_status, detail) "
                     "VALUES (%s, %s, %s, 'decision', %s, %s, %s::jsonb)",
                     [record.case_id, record.review_id, record.analyst_id, case["status"], to_status,
                      json.dumps({"decision": record.decision, "agent_recommendation": record.agent_recommendation,
                                  "overridden": record.overridden})])
    return True
