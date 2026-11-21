"""case_db through SQLAlchemy 2.x: the engine, and an ORM model of each table.

The models mirror services/case/schema.sql: the same tables (cases and their evidence,
the decisions and the audit log, and the mock schema's records), with the same column
names, types, primary keys and foreign keys. schema.sql still creates the tables, so
the service never calls metadata.create_all, and a change to a table goes into both
files (tests/test_db.py checks that they agree). The checks and indexes live only in
schema.sql.

The driver is psycopg 3 (postgresql+psycopg://). raw_text, page_text and source_case
are deferred: they're loaded only if asked for by name, and no endpoint asks.
"""

import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    ARRAY,
    URL,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Double,
    Engine,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

POOL_SIZE = 5        # connections, as many as psycopg_pool's max_size before
STARTUP_WAIT = 30    # seconds to wait for the database when the service starts

CASES_FK = "cases.case_id"


def make_engine(*, host: str, port: str | int, password: str) -> Engine:
    """The engine for case_db, as case_svc: at most POOL_SIZE connections, each checked before use."""
    url = URL.create("postgresql+psycopg", username="case_svc", password=password, host=host, port=int(port),
                     database="case_db")
    return create_engine(url, pool_size=POOL_SIZE, max_overflow=0, pool_pre_ping=True)


def wait_for_database(engine: Engine, timeout: float = STARTUP_WAIT) -> None:
    """Connects once, trying again for up to timeout seconds while the database starts."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


def run_schema(engine: Engine, schema: Path) -> None:
    """Runs schema.sql in one go. psycopg runs several statements in one call only when
    there are no parameters, so the script goes to the driver's own connection."""
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cursor:
            cursor.execute(schema.read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()


class Base(DeclarativeBase):
    pass


# ---------- Cases (case.json, served as CaseInput) ----------

class Case(Base):
    __tablename__ = "cases"

    case_id: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text)
    flag_reasons: Mapped[list[str]] = mapped_column(ARRAY(Text))
    legal_name: Mapped[str] = mapped_column(Text)
    dba: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text)
    tin_type: Mapped[str | None] = mapped_column(Text)
    tin_last4: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    state_entity_id: Mapped[str | None] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    formation_date: Mapped[date | None] = mapped_column(Date)
    checks: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CaseOwner(Base):
    __tablename__ = "case_owners"

    case_id: Mapped[str] = mapped_column(Text, ForeignKey(CASES_FK, ondelete="CASCADE"), primary_key=True)
    owner_ref: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)


class Document(Base):
    __tablename__ = "documents"

    case_id: Mapped[str] = mapped_column(Text, ForeignKey(CASES_FK, ondelete="CASCADE"), primary_key=True)
    doc_id: Mapped[str] = mapped_column(Text, primary_key=True)
    type: Mapped[str] = mapped_column(Text)
    extracted: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    confidence: Mapped[float] = mapped_column(Double)
    raw_text: Mapped[str | None] = mapped_column(Text, deferred=True)       # never returned


class BankVerification(Base):
    __tablename__ = "bank_verifications"
    __table_args__ = (UniqueConstraint("case_id"),)                          # a case has at most one

    case_id: Mapped[str] = mapped_column(Text, ForeignKey(CASES_FK, ondelete="CASCADE"), primary_key=True)
    bank_id: Mapped[str] = mapped_column(Text, primary_key=True)
    account_holder: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    verified_at: Mapped[date | None] = mapped_column(Date)


class WebSnapshot(Base):
    __tablename__ = "web_snapshots"

    case_id: Mapped[str] = mapped_column(Text, ForeignKey(CASES_FK, ondelete="CASCADE"), primary_key=True)
    web_id: Mapped[str] = mapped_column(Text, primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    captured_at: Mapped[date] = mapped_column(Date)
    extracted: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    page_text: Mapped[str | None] = mapped_column(Text, deferred=True)      # never returned


class OwnerKyc(Base):
    __tablename__ = "owner_kyc"
    __table_args__ = (ForeignKeyConstraint(["case_id", "owner_ref"],
                                           ["case_owners.case_id", "case_owners.owner_ref"], ondelete="CASCADE"),)

    case_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kyc_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_ref: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)


# ---------- Step 11: decisions and the audit log (no cascade: history can't be deleted) ----------

class Decision(Base):
    __tablename__ = "decisions"

    review_id: Mapped[str] = mapped_column(Text, primary_key=True)
    case_id: Mapped[str] = mapped_column(Text, ForeignKey(CASES_FK))
    decision: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text, server_default=text("''"))
    analyst_id: Mapped[str] = mapped_column(Text)
    agent_recommendation: Mapped[str] = mapped_column(Text)
    overridden: Mapped[bool] = mapped_column(Boolean)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    case_id: Mapped[str] = mapped_column(Text, ForeignKey(CASES_FK))
    review_id: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------- mock: the mocked registry, DBA, commercial and IRS records (world.json) ----------

MOCK = {"schema": "mock"}


class RegistryEntity(Base):
    __tablename__ = "registry_entities"
    __table_args__ = MOCK

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    state_entity_id: Mapped[str] = mapped_column(Text, primary_key=True)
    legal_name: Mapped[str] = mapped_column(Text)
    former_names: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    entity_type: Mapped[str] = mapped_column(Text)
    entity_status: Mapped[str] = mapped_column(Text)
    formation_date: Mapped[date | None] = mapped_column(Date)
    registered_address: Mapped[str] = mapped_column(Text)
    officers: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'"))
    source_case: Mapped[str] = mapped_column(Text, ForeignKey(CASES_FK, ondelete="CASCADE"), deferred=True)


class RegistryFiling(Base):
    __tablename__ = "registry_filings"
    __table_args__ = (ForeignKeyConstraint(["state", "state_entity_id"],
                                           ["mock.registry_entities.state", "mock.registry_entities.state_entity_id"],
                                           ondelete="CASCADE"), MOCK)

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[str] = mapped_column(Text)
    state_entity_id: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text)
    effective_date: Mapped[date] = mapped_column(Date)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)


class DbaFiling(Base):
    __tablename__ = "dba_filings"
    __table_args__ = MOCK

    filing_id: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[str] = mapped_column(Text)
    county: Mapped[str | None] = mapped_column(Text)                         # null for a state-level filing
    dba: Mapped[str] = mapped_column(Text)
    registrant: Mapped[str] = mapped_column(Text)
    registrant_type: Mapped[str] = mapped_column(Text)
    registrant_state_entity_id: Mapped[str | None] = mapped_column(Text)
    filed: Mapped[date] = mapped_column(Date)
    expires: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text)
    source_case: Mapped[str] = mapped_column(Text, ForeignKey(CASES_FK, ondelete="CASCADE"), deferred=True)


class CommercialProfile(Base):
    __tablename__ = "commercial_profiles"
    __table_args__ = MOCK

    profile_id: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[str] = mapped_column(Text)                                 # filtered on, never returned
    business_name: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text)
    first_seen: Mapped[date] = mapped_column(Date)
    last_updated: Mapped[date] = mapped_column(Date)
    source_case: Mapped[str] = mapped_column(Text, ForeignKey(CASES_FK, ondelete="CASCADE"), deferred=True)


class TinRecord(Base):
    __tablename__ = "tin_records"
    __table_args__ = MOCK

    case_id: Mapped[str] = mapped_column(Text, ForeignKey(CASES_FK, ondelete="CASCADE"), primary_key=True)
    tin_type: Mapped[str] = mapped_column(Text)
    tin_last4: Mapped[str] = mapped_column(Text)
    irs_status: Mapped[str] = mapped_column(Text)
    irs_name: Mapped[str | None] = mapped_column(Text)
