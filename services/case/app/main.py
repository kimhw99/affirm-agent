"""The case service's HTTP API.

GET /cases/{case_id} serves a whole CaseInput, and endpoints 1-10 serve the records
the agent's workers look up (docs/case-service-endpoints.md). All of them are
read-only and need the service token; GET /health doesn't.

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
from pathlib import Path
from typing import Annotated, Iterator

import psycopg
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from kyb_contracts.case_input import BankVerification, CaseInput, Document, OwnerKYC, WebSnapshot
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


app.include_router(router)
