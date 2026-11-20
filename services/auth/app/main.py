"""The auth service: logs users in and issues JWTs (docs/auth.md).

POST /auth/login checks a username and password and returns a JWT that carries the
user's role. GET /auth/me returns the user a JWT belongs to. Users live in auth_db.

When it starts, it connects to auth_db as auth_svc with AUTH_DB_PASSWORD, at
AUTH_DB_HOST and AUTH_DB_PORT (by default localhost and POSTGRES_HOST_PORT), runs
schema.sql, and creates or updates the demo users whose passwords are set
(DEMO_ANALYST_PASSWORD, DEMO_SENIOR_PASSWORD). JWT_SECRET, at least 32 characters,
signs the tokens, and JWT_TTL_MINUTES (480 by default) sets how long they last.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Iterator

import jwt
import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from kyb_contracts.auth import LoginRequest, LoginResponse, User

from .security import DUMMY_HASH, hash_password, issue_token, read_token, verify_password

SCHEMA = Path(__file__).resolve().parents[1] / "schema.sql"
USER_FIELDS = ("user_id", "username", "display_name", "role")
USER_COLUMNS = ", ".join(USER_FIELDS)

# The demo users: user_id, username, display name, role, and the variable with the password.
DEMO_USERS = [
    ("u_analyst_01", "analyst", "Demo Analyst", "analyst", "DEMO_ANALYST_PASSWORD"),
    ("u_senior_01", "senior", "Demo Senior Analyst", "senior_analyst", "DEMO_SENIOR_PASSWORD"),
]


def jwt_secret() -> str:
    secret = os.environ.get("JWT_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("JWT_SECRET must be at least 32 characters (see .env.example)")
    return secret


def save_demo_users(conn) -> None:
    """Creates each demo user whose password is set, or updates it to match the environment."""
    for user_id, username, name, role, variable in DEMO_USERS:
        password = os.environ.get(variable)
        if password:
            conn.execute(
                f"INSERT INTO users ({USER_COLUMNS}, password_hash) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, "
                "display_name = EXCLUDED.display_name, role = EXCLUDED.role, password_hash = EXCLUDED.password_hash",
                [user_id, username, name, role, hash_password(password)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    password = os.environ.get("AUTH_DB_PASSWORD")
    if not password:
        raise RuntimeError("set AUTH_DB_PASSWORD (see .env.example)")
    app.state.secret = jwt_secret()
    app.state.ttl = int(os.environ.get("JWT_TTL_MINUTES") or 480) * 60
    conninfo = make_conninfo(
        host=os.environ.get("AUTH_DB_HOST", "localhost"),
        port=os.environ.get("AUTH_DB_PORT") or os.environ.get("POSTGRES_HOST_PORT") or "55432",
        dbname="auth_db", user="auth_svc", password=password)
    pool = ConnectionPool(conninfo, min_size=1, max_size=5, kwargs={"row_factory": dict_row}, open=False)
    pool.open(wait=True, timeout=30)
    with pool.connection() as conn:
        conn.execute(SCHEMA.read_text(encoding="utf-8"))
        save_demo_users(conn)
    app.state.pool = pool
    try:
        yield
    finally:
        pool.close()


app = FastAPI(title="Auth service", lifespan=lifespan)


def connection(request: Request) -> Iterator[psycopg.Connection]:
    """A pooled database connection for one request."""
    with request.app.state.pool.connection() as conn:
        yield conn


Conn = Annotated[psycopg.Connection, Depends(connection)]


@app.get("/health")
def health(conn: Conn) -> dict[str, str]:
    conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, conn: Conn):
    row = conn.execute(f"SELECT {USER_COLUMNS}, password_hash FROM users WHERE username = %s",
                       [body.username]).fetchone()
    # The same check and the same answer either way, so a response doesn't tell which was wrong.
    password_ok = verify_password(body.password, row["password_hash"] if row else DUMMY_HASH)
    if row is None or not password_ok:
        raise HTTPException(401, "wrong username or password")
    user = User.model_validate({field: row[field] for field in USER_FIELDS})  # never the password hash
    token = issue_token(user, request.app.state.secret, request.app.state.ttl)
    return LoginResponse(access_token=token, expires_in=request.app.state.ttl, user=user)


@app.get("/auth/me", response_model=User)
def me(request: Request, conn: Conn, authorization: Annotated[str | None, Header()] = None):
    scheme, _, token = (authorization or "").partition(" ")
    try:
        if scheme.lower() != "bearer":
            raise jwt.InvalidTokenError("not a Bearer token")
        claims = read_token(token, request.app.state.secret)
    except (jwt.InvalidTokenError, ValidationError):
        raise HTTPException(401, "log in first", headers={"WWW-Authenticate": "Bearer"}) from None
    row = conn.execute(f"SELECT {USER_COLUMNS} FROM users WHERE user_id = %s", [claims.sub]).fetchone()
    if row is None:
        raise HTTPException(401, "this user no longer exists", headers={"WWW-Authenticate": "Bearer"})
    return User.model_validate({field: row[field] for field in USER_FIELDS})
