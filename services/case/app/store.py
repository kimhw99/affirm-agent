"""Reads from case_db, returned as the kyb_contracts models the endpoints serve, and
the one write: saving the analyst's decision (step 11).

Every function takes a SQLAlchemy Session (main.py opens one per request). The
queries are built from the ORM models in app/db.py and read through result.mappings(),
so each row is keyed by column name. A lookup returns None when nothing matches; a
search returns a list, possibly empty. Each query selects only the columns a contract
has, so source_case, raw_text and page_text never leave the database.

Searches work like FakeCaseClient in services/agent/app/clients/case_client.py, so the
agent's offline tests and the real service find the same records. A field matches
when any significant word of the query appears in it, ignoring case. Every criterion
given must match, and at most 20 rows come back, best matches first. Unlike the fake,
there is no fuzzy matching of misspelled words yet. The searches are SQL text, for
Postgres's ILIKE, unnest and jsonb_array_elements; the query's words go in only as
bound parameters, never into the SQL itself.
"""

import re
from datetime import datetime, timezone

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.orm import Session

from kyb_contracts.case_input import BankVerification, CaseInput, Document, OwnerKYC, WebSnapshot
from kyb_contracts.decision import STATUS_AFTER, DecisionRecord
from kyb_contracts.world import CommercialProfile, DbaFiling, Filing, RegistryEntity, TinRecord

from . import db

SEARCH_LIMIT = 20

# Words too common to search on (entity designators and joiners), as in FakeCaseClient.
STOPWORDS = {
    "the", "and", "of", "llc", "lc", "pllc", "inc", "incorporated", "corp", "corporation",
    "co", "company", "ltd", "limited", "llp", "lp", "pc", "dba",
}

APPLICATION = ("legal_name", "dba", "address", "tin_type", "tin_last4", "state", "state_entity_id",
               "entity_type", "formation_date")

# The columns each contract has, named as the contract names them.
CASE = (db.Case.case_id, db.Case.status, db.Case.flag_reasons, db.Case.checks,
        *(getattr(db.Case, field) for field in APPLICATION))
OWNERS = (db.CaseOwner.owner_ref.label("ref"), db.CaseOwner.name, db.CaseOwner.role)
DOCUMENTS = (db.Document.doc_id.label("id"), db.Document.type, db.Document.extracted, db.Document.confidence)
BANK = (db.BankVerification.bank_id.label("id"), db.BankVerification.account_holder, db.BankVerification.status,
        db.BankVerification.verified_at)
WEB = (db.WebSnapshot.web_id.label("id"), db.WebSnapshot.url, db.WebSnapshot.captured_at, db.WebSnapshot.extracted)
KYC = (db.OwnerKyc.kyc_id.label("id"), db.OwnerKyc.owner_ref, db.OwnerKyc.result)
ENTITY = (db.RegistryEntity.state, db.RegistryEntity.state_entity_id, db.RegistryEntity.legal_name,
          db.RegistryEntity.former_names, db.RegistryEntity.entity_type, db.RegistryEntity.entity_status,
          db.RegistryEntity.formation_date, db.RegistryEntity.registered_address, db.RegistryEntity.officers)
FILING = (db.RegistryFiling.event_id, db.RegistryFiling.event_type, db.RegistryFiling.effective_date,
          db.RegistryFiling.old_value, db.RegistryFiling.new_value)
DBA = (db.DbaFiling.filing_id, db.DbaFiling.state, db.DbaFiling.county, db.DbaFiling.dba, db.DbaFiling.registrant,
       db.DbaFiling.registrant_type, db.DbaFiling.registrant_state_entity_id, db.DbaFiling.filed,
       db.DbaFiling.expires, db.DbaFiling.status)
COMMERCIAL = (db.CommercialProfile.profile_id, db.CommercialProfile.business_name, db.CommercialProfile.address,
              db.CommercialProfile.first_seen, db.CommercialProfile.last_updated)
TIN = (db.TinRecord.tin_type, db.TinRecord.tin_last4, db.TinRecord.irs_status, db.TinRecord.irs_name)

# A county for comparing: "Cook County" and "cook" are the same, as in FakeCaseClient.
COUNTY = r"lower(regexp_replace(trim({}), '\s+county$', '', 'i'))"


def search_words(text: str | None) -> list[str]:
    """The words of a query worth matching, picked as FakeCaseClient picks them."""
    words = re.sub(r"[^0-9a-z]+", " ", (text or "").casefold().replace("&", " and ")).split()
    significant = [w for w in words if w not in STOPWORDS and len(w) > 1]
    return list(dict.fromkeys(significant or words))


def _rows(session: Session, statement) -> list[dict]:
    return [dict(row) for row in session.execute(statement).mappings()]


def _one(session: Session, model, statement):
    row = session.execute(statement).mappings().first()
    return None if row is None else model.model_validate(dict(row))


def _natural(column) -> tuple:
    """Sorted by length first, so D2 comes before D10."""
    return func.length(column), column


def _names(columns) -> str:
    return ", ".join(column.key for column in columns)


def search_sql(table: str, columns: str, where: str, order: str,
               criteria: list[tuple[str, str | None]]) -> tuple[str, dict[str, str]] | None:
    """The SQL of a search and its parameters, or None when there is nothing to search on.

    A criterion is a condition on one word, with {pattern} where that word's ILIKE pattern
    goes, and the query text; a criterion without text is skipped. A row's score is how
    many query words it matches. The words are bound parameters, never part of the SQL.
    """
    counts, params = [], {}
    for c, (condition, query) in enumerate(criteria):
        if not query:
            continue
        words = search_words(query)
        if not words:
            return None  # nothing in the text to match on
        terms = []
        for w, word in enumerate(words):
            name = f"c{c}_w{w}"
            params[name] = f"%{word}%"
            terms.append(f"({condition.format(pattern=':' + name)})::int")
        counts.append(" + ".join(terms))
    if not counts:
        return None
    hits = [f"hits{i}" for i in range(len(counts))]
    sql = (f"SELECT {columns} FROM ("
           f"SELECT *, {', '.join(f'{count} AS {hit}' for count, hit in zip(counts, hits))} "
           f"FROM {table} WHERE {where}) AS candidates "
           f"WHERE {' AND '.join(f'{hit} > 0' for hit in hits)} "
           f"ORDER BY {' + '.join(hits)} DESC, {order} LIMIT {SEARCH_LIMIT}")
    return sql, params


def _search(session: Session, table, columns, where: str, where_params: dict, order: str,
            criteria: list[tuple[str, str | None]]) -> list[dict]:
    """The rows of table that pass where and match every criterion given, best first."""
    built = search_sql(table.fullname, _names(columns), where, order, criteria)
    if built is None:
        return []
    sql, params = built
    return [dict(row) for row in session.execute(text(sql), params | where_params).mappings()]


# ---------- GET /cases/{case_id} ----------

def get_case(session: Session, case_id: str) -> CaseInput | None:
    case = session.execute(select(*CASE).where(db.Case.case_id == case_id)).mappings().first()
    if case is None:
        return None
    case = dict(case)
    application = {field: case.pop(field) for field in APPLICATION}
    application["owners"] = _rows(session, select(*OWNERS).where(db.CaseOwner.case_id == case_id)
                                  .order_by(*_natural(db.CaseOwner.owner_ref)))
    bank = _rows(session, select(*BANK).where(db.BankVerification.case_id == case_id))
    return CaseInput.model_validate(case | {
        "application": application,
        "documents": _rows(session, select(*DOCUMENTS).where(db.Document.case_id == case_id)
                           .order_by(*_natural(db.Document.doc_id))),
        "bank_verification": bank[0] if bank else None,
        "web_snapshot": _rows(session, select(*WEB).where(db.WebSnapshot.case_id == case_id)
                              .order_by(*_natural(db.WebSnapshot.web_id))),
        "owner_kyc": _rows(session, select(*KYC).where(db.OwnerKyc.case_id == case_id)
                           .order_by(*_natural(db.OwnerKyc.kyc_id))),
    })


# ---------- Records worker: official and third-party data (endpoints 1-7) ----------

def search_registry(session: Session, state: str, name: str | None = None, officer: str | None = None,
                    address: str | None = None) -> list[RegistryEntity]:
    """Endpoint 1. A name matches the legal name or any former name."""
    rows = _search(session, db.RegistryEntity.__table__, ENTITY, "state = upper(:state)", {"state": state},
                   "legal_name", [
        ("legal_name ILIKE {pattern} OR EXISTS (SELECT 1 FROM unnest(former_names) AS f(n) WHERE f.n ILIKE {pattern})",
         name),
        ("EXISTS (SELECT 1 FROM jsonb_array_elements(officers) AS o(v) WHERE o.v->>'name' ILIKE {pattern})", officer),
        ("registered_address ILIKE {pattern}", address),
    ])
    return [RegistryEntity.model_validate(row) for row in rows]


def get_registry_entity(session: Session, state: str, state_entity_id: str) -> RegistryEntity | None:
    """Endpoint 2."""
    return _one(session, RegistryEntity, select(*ENTITY).where(
        db.RegistryEntity.state == func.upper(state), db.RegistryEntity.state_entity_id == state_entity_id))


def get_filings(session: Session, state: str, state_entity_id: str) -> list[Filing] | None:
    """Endpoint 3: the entity's filing history, oldest first, or None if there's no such entity."""
    if get_registry_entity(session, state, state_entity_id) is None:
        return None
    rows = _rows(session, select(*FILING).where(
        db.RegistryFiling.state == func.upper(state), db.RegistryFiling.state_entity_id == state_entity_id)
        .order_by(db.RegistryFiling.effective_date, db.RegistryFiling.event_id))
    return [Filing.model_validate(row) for row in rows]


def search_dba(session: Session, state: str, name: str, county: str | None = None) -> list[DbaFiling]:
    """Endpoint 4. Without a county it searches state-level filings and every county."""
    where, params = "state = upper(:state)", {"state": state}
    if county:
        where += f" AND {COUNTY.format('county')} = {COUNTY.format(':county')}"
        params["county"] = county
    rows = _search(session, db.DbaFiling.__table__, DBA, where, params, "dba", [("dba ILIKE {pattern}", name)])
    return [DbaFiling.model_validate(row) for row in rows]


def search_commercial(session: Session, state: str, name: str | None = None,
                      address: str | None = None) -> list[CommercialProfile]:
    """Endpoint 5. The state is a filter only; profiles come back without it."""
    rows = _search(session, db.CommercialProfile.__table__, COMMERCIAL, "state = upper(:state)", {"state": state},
                   "business_name", [("business_name ILIKE {pattern}", name), ("address ILIKE {pattern}", address)])
    return [CommercialProfile.model_validate(row) for row in rows]


def get_tin_record(session: Session, case_id: str) -> TinRecord | None:
    """Endpoint 6: None when the case has no submitted TIN, or doesn't exist."""
    return _one(session, TinRecord, select(*TIN).where(db.TinRecord.case_id == case_id))


def get_owner_kyc(session: Session, case_id: str, kyc_id: str) -> OwnerKYC | None:
    """Endpoint 7."""
    return _one(session, OwnerKYC, select(*KYC).where(db.OwnerKyc.case_id == case_id, db.OwnerKyc.kyc_id == kyc_id))


# ---------- Evidence worker: merchant evidence (endpoints 8-10) ----------

def get_document(session: Session, case_id: str, doc_id: str) -> Document | None:
    """Endpoint 8, without the OCR text."""
    return _one(session, Document, select(*DOCUMENTS).where(db.Document.case_id == case_id,
                                                             db.Document.doc_id == doc_id))


def get_bank_verification(session: Session, case_id: str, bank_id: str) -> BankVerification | None:
    """Endpoint 9."""
    return _one(session, BankVerification, select(*BANK).where(db.BankVerification.case_id == case_id,
                                                                db.BankVerification.bank_id == bank_id))


def get_web_snapshot(session: Session, case_id: str, web_id: str) -> WebSnapshot | None:
    """Endpoint 10, without the page text."""
    return _one(session, WebSnapshot, select(*WEB).where(db.WebSnapshot.case_id == case_id,
                                                          db.WebSnapshot.web_id == web_id))


# ---------- Step 11: the analyst's decision ----------

DECISION = (db.Decision.review_id, db.Decision.case_id, db.Decision.decision, db.Decision.note,
            db.Decision.analyst_id, db.Decision.agent_recommendation, db.Decision.overridden, db.Decision.decided_at)

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


def save_decision(session: Session, record: DecisionRecord) -> bool:
    """Step 11: saves the decision, the case's new status and an audit log entry, all or nothing.

    It runs in a transaction of its own, committed before it returns, so the session
    must not have one open yet. Returns False, changing nothing, when this review's
    decision was already saved with the same content, so a retry is safe. Raises
    CaseNotFound or DecisionConflict.
    """
    to_status = STATUS_AFTER[record.decision].value
    with session.begin():
        # Locks the case's row, so two decisions for one case can't both pass the checks.
        status = session.execute(select(db.Case.status).where(db.Case.case_id == record.case_id)
                                 .with_for_update()).scalar_one_or_none()
        if status is None:
            raise CaseNotFound(record.case_id)
        saved = session.execute(select(*DECISION).where(db.Decision.review_id == record.review_id)).mappings().first()
        if saved is not None:
            if same_decision(DecisionRecord.model_validate(dict(saved)), record):
                return False
            raise DecisionConflict(f"review {record.review_id} already has a different decision")
        if status not in DECIDABLE:
            raise DecisionConflict(f"case {record.case_id} is {status}, so it can't take a decision")
        session.execute(insert(db.Decision).values(
            review_id=record.review_id, case_id=record.case_id, decision=record.decision, note=record.note,
            analyst_id=record.analyst_id, agent_recommendation=record.agent_recommendation,
            overridden=record.overridden, decided_at=_utc(record.decided_at)))
        session.execute(update(db.Case).where(db.Case.case_id == record.case_id)
                        .values(status=to_status, updated_at=func.now()))
        session.execute(insert(db.AuditLog).values(
            case_id=record.case_id, review_id=record.review_id, actor_id=record.analyst_id, action="decision",
            from_status=status, to_status=to_status,
            detail={"decision": record.decision, "agent_recommendation": record.agent_recommendation,
                    "overridden": record.overridden}))
    return True


# ---------- For the review app ----------

SUMMARY = (db.Case.case_id, db.Case.status, db.Case.legal_name, db.Case.dba, db.Case.state, db.Case.flag_reasons,
           db.Case.created_at, db.Case.updated_at)


def list_cases(session: Session, status: str | None = None) -> list[dict]:
    """The case queue: every case, or the ones with this status, oldest first."""
    statement = select(*SUMMARY).order_by(db.Case.created_at, db.Case.case_id)
    if status is not None:
        statement = statement.where(db.Case.status == status)
    return _rows(session, statement)


def list_decisions(session: Session, case_id: str) -> list[DecisionRecord] | None:
    """The case's decisions, oldest first, or None if there's no such case."""
    if session.execute(select(db.Case.case_id).where(db.Case.case_id == case_id)).first() is None:
        return None
    rows = _rows(session, select(*DECISION).where(db.Decision.case_id == case_id)
                 .order_by(db.Decision.decided_at, db.Decision.review_id))
    return [DecisionRecord.model_validate(row) for row in rows]


def override_counts(session: Session) -> list[dict]:
    """For each recommendation of the agent: how many decisions there were, and how many overrode it."""
    return _rows(session, select(db.Decision.agent_recommendation.label("recommendation"),
                                 func.count().label("decisions"),
                                 func.count().filter(db.Decision.overridden).label("overridden"))
                 .group_by(db.Decision.agent_recommendation).order_by(db.Decision.agent_recommendation))
