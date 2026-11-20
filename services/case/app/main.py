"""The case service's HTTP API.

GET /cases/{case_id} serves a whole CaseInput, and endpoints 1-10 serve the records
the agent's workers look up (docs/case-service-endpoints.md). POST
/cases/{case_id}/decisions saves the analyst's decision (step 11). For the review app,
GET /cases lists the case queue, GET /cases/{case_id}/decisions a case's decisions, and
GET /metrics/override-rate how often analysts override the agent. All of them need
the service token; GET /health doesn't.

Until the auth service issues service JWTs (BEAC-14), the token is a shared secret:
the agent sends CASE_SERVICE_TOKEN as a Bearer token, and this service compares it
with its own CASE_SERVICE_TOKEN.

When it starts, it connects to case_db as case_svc with CASE_DB_PASSWORD, at
CASE_DB_HOST and CASE_DB_PORT (by default localhost and POSTGRES_HOST_PORT), and runs
schema.sql. docker compose up -d starts it next to the database. To run it on this
machine instead, from services/case: uvicorn app.main:app --port 8001
"""

import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Iterator

import psycopg
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict

from kyb_contracts.case_input import BankVerification, CaseInput, CaseStatus, Document, OwnerKYC, WebSnapshot
from kyb_contracts.decision import STATUS_AFTER, DecisionRecord
from kyb_contracts.world import CommercialProfile, DbaFiling, Filing, RegistryEntity, TinRecord

from . import store

SCHEMA = Path(__file__).resolve().parents[1] / "schema.sql"


@asynccontextmanager
async def lifespan(app: FastAPI):
    token = os.environ.get("CASE_SERVICE_TOKEN")
    password = os.environ.get("CASE_DB_PASSWORD")
    missing = [name for name, value in (("CASE_SERVICE_TOKEN", token), ("CASE_DB_PASSWORD", password))
               if not value]
    if missing:
        raise RuntimeError(f"set {' and '.join(missing)} (see .env.example)")
    conninfo = make_conninfo(
        host=os.environ.get("CASE_DB_HOST", "localhost"),
        port=os.environ.get("CASE_DB_PORT") or os.environ.get("POSTGRES_HOST_PORT") or "55432",
        dbname="case_db", user="case_svc", password=password)
    pool = ConnectionPool(conninfo, min_size=1, max_size=5, kwargs={"row_factory": dict_row}, open=False)
    pool.open(wait=True, timeout=30)
    with pool.connection() as conn:
        conn.execute(SCHEMA.read_text(encoding="utf-8"))
    app.state.token, app.state.pool = token, pool
    try:
        yield
    finally:
        pool.close()


app = FastAPI(title="Case service", lifespan=lifespan)


def connection(request: Request) -> Iterator[psycopg.Connection]:
    """A pooled database connection for one request."""
    with request.app.state.pool.connection() as conn:
        yield conn


def require_token(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.encode(), request.app.state.token.encode()):
        raise HTTPException(401, "a valid service token is required", headers={"WWW-Authenticate": "Bearer"})


Conn = Annotated[psycopg.Connection, Depends(connection)]
State = Annotated[str, Query(pattern=r"^[A-Za-z]{2}$")]
router = APIRouter(dependencies=[Depends(require_token)])


def found(value, what: str):
    """value, or a 404 when the lookup found nothing."""
    if value is None:
        raise HTTPException(404, f"no such {what}")
    return value


@app.get("/health")
def health(conn: Conn) -> dict[str, str]:
    conn.execute("SELECT 1")
    return {"status": "ok"}


@router.get("/cases/{case_id}", response_model=CaseInput)
def get_case(case_id: str, conn: Conn):
    return found(store.get_case(conn, case_id), "case")


# ---------- Records worker: official and third-party data (endpoints 1-7) ----------

@router.get("/registry/search", response_model=list[RegistryEntity])
def search_registry(conn: Conn, state: State, name: str | None = None, officer: str | None = None,
                    address: str | None = None):
    if not (name or officer or address):
        raise HTTPException(422, "give at least one of name, officer or address")
    return store.search_registry(conn, state, name, officer, address)


@router.get("/registry/entities/{state}/{state_entity_id}", response_model=RegistryEntity)
def get_registry_entity(state: str, state_entity_id: str, conn: Conn):
    return found(store.get_registry_entity(conn, state, state_entity_id), "registry entity")


@router.get("/registry/entities/{state}/{state_entity_id}/filings", response_model=list[Filing])
def get_filings(state: str, state_entity_id: str, conn: Conn):
    return found(store.get_filings(conn, state, state_entity_id), "registry entity")


@router.get("/registry/dba", response_model=list[DbaFiling])
def search_dba(conn: Conn, state: State, name: str, county: str | None = None):
    return store.search_dba(conn, state, name, county)


@router.get("/commercial/search", response_model=list[CommercialProfile])
def search_commercial(conn: Conn, state: State, name: str | None = None, address: str | None = None):
    if not (name or address):
        raise HTTPException(422, "give name or address")
    return store.search_commercial(conn, state, name, address)


@router.get("/cases/{case_id}/tin-record", response_model=TinRecord)
def get_tin_record(case_id: str, conn: Conn):
    return found(store.get_tin_record(conn, case_id), "TIN record")


@router.get("/cases/{case_id}/owner-kyc/{kyc_id}", response_model=OwnerKYC)
def get_owner_kyc(case_id: str, kyc_id: str, conn: Conn):
    return found(store.get_owner_kyc(conn, case_id, kyc_id), "KYC result")


# ---------- Evidence worker: merchant evidence (endpoints 8-10) ----------

@router.get("/cases/{case_id}/documents/{doc_id}", response_model=Document)
def get_document(case_id: str, doc_id: str, conn: Conn):
    return found(store.get_document(conn, case_id, doc_id), "document")


@router.get("/cases/{case_id}/bank-verification/{bank_id}", response_model=BankVerification)
def get_bank_verification(case_id: str, bank_id: str, conn: Conn):
    return found(store.get_bank_verification(conn, case_id, bank_id), "bank verification")


@router.get("/cases/{case_id}/web-snapshots/{web_id}", response_model=WebSnapshot)
def get_web_snapshot(case_id: str, web_id: str, conn: Conn):
    return found(store.get_web_snapshot(conn, case_id, web_id), "web snapshot")


# ---------- Step 11: the analyst's decision ----------

class SaveDecision(BaseModel):
    """Body of POST /cases/{case_id}/decisions, and its response."""

    model_config = ConfigDict(extra="forbid")

    decision_record: DecisionRecord
    case_status: CaseStatus | None = None  # the status the sender expects; it must be STATUS_AFTER's


@router.post("/cases/{case_id}/decisions", status_code=201, response_model=SaveDecision)
def save_decision(case_id: str, body: SaveDecision, conn: Conn, response: Response):
    """Saves the decision, sets the case's status from it and writes the audit log, in one transaction."""
    record = body.decision_record
    if record.case_id != case_id:
        raise HTTPException(422, f"decision_record.case_id is {record.case_id}, but the path is for {case_id}")
    status = STATUS_AFTER[record.decision]
    if body.case_status not in (None, status):
        raise HTTPException(422, f"{record.decision} sets the case to {status.value}, not {body.case_status.value}")
    try:
        created = store.save_decision(conn, record)
    except store.CaseNotFound:
        raise HTTPException(404, "no such case") from None
    except store.DecisionConflict as error:
        raise HTTPException(409, str(error)) from None
    if not created:
        response.status_code = 200  # already saved: a retry
    return SaveDecision(decision_record=record, case_status=status)


# ---------- For the review app ----------

class CaseSummary(BaseModel):
    """One row of the case queue."""

    case_id: str
    status: CaseStatus
    legal_name: str
    dba: str | None
    state: str
    flag_reasons: list[str]
    created_at: datetime
    updated_at: datetime


class RecommendationOverrides(BaseModel):
    recommendation: str
    decisions: int
    overridden: int
    rate: float | None  # overridden / decisions, or None before the first decision


class OverrideRate(BaseModel):
    """How often analysts decide against the agent's recommendation (BEAC-20)."""

    decisions: int
    overridden: int
    rate: float | None
    by_recommendation: list[RecommendationOverrides]


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


@router.get("/cases", response_model=list[CaseSummary])
def list_cases(conn: Conn, status: CaseStatus | None = None):
    """The case queue: every case, or the ones with this status, oldest first."""
    return store.list_cases(conn, status.value if status else None)


@router.get("/cases/{case_id}/decisions", response_model=list[DecisionRecord])
def list_decisions(case_id: str, conn: Conn):
    """The case's decisions, oldest first."""
    return found(store.list_decisions(conn, case_id), "case")


@router.get("/metrics/override-rate", response_model=OverrideRate)
def override_rate(conn: Conn):
    groups = [RecommendationOverrides(**row, rate=_rate(row["overridden"], row["decisions"]))
              for row in store.override_counts(conn)]
    decisions = sum(group.decisions for group in groups)
    overridden = sum(group.overridden for group in groups)
    return OverrideRate(decisions=decisions, overridden=overridden, rate=_rate(overridden, decisions),
                        by_recommendation=groups)


app.include_router(router)
