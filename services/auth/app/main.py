"""The auth service: logs users in and issues JWTs (docs/auth.md).

POST /auth/login checks a username and password and returns a JWT that carries the
user's role. GET /auth/me returns the user a JWT belongs to. Users live in auth_db,
reached through SQLAlchemy 2.x with the psycopg 3 driver: app/db.py has the engine and
the ORM model, and app/store.py every query.

When it starts, it connects to auth_db as auth_svc with AUTH_DB_PASSWORD, at
AUTH_DB_HOST and AUTH_DB_PORT (by default localhost and POSTGRES_HOST_PORT), runs
schema.sql, and creates or updates the demo users whose passwords are set
(DEMO_ANALYST_PASSWORD, DEMO_SENIOR_PASSWORD). JWT_SECRET, at least 32 characters,
signs the tokens, and JWT_TTL_MINUTES (480 by default) sets how long they last.

It logs (app/logs.py, at LOG_LEVEL) each login, each refused login with the username
only, each refused /auth/me, and at startup the schema and the demo users it saved,
each line with the request's X-Request-ID. Never a password, a hash or a token.
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Iterator

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from kyb_contracts.auth import LoginRequest, LoginResponse, User

from . import db, logs, store
from .security import DUMMY_HASH, issue_token, read_token, verify_password

SCHEMA = Path(__file__).resolve().parents[1] / "schema.sql"
USER_FIELDS = ("user_id", "username", "display_name", "role")

logs.configure()
log = logging.getLogger(__name__)


def jwt_secret() -> str:
    secret = os.environ.get("JWT_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("JWT_SECRET must be at least 32 characters (see .env.example)")
    return secret


def public(account: db.User) -> User:
    """The user as the API returns it: these fields only, never the password hash."""
    return User.model_validate({field: getattr(account, field) for field in USER_FIELDS})


@asynccontextmanager
async def lifespan(app: FastAPI):
    password = os.environ.get("AUTH_DB_PASSWORD")
    if not password:
        raise RuntimeError("set AUTH_DB_PASSWORD (see .env.example)")
    app.state.secret = jwt_secret()
    app.state.ttl = int(os.environ.get("JWT_TTL_MINUTES") or 480) * 60
    engine = db.make_engine(
        host=os.environ.get("AUTH_DB_HOST", "localhost"),
        port=os.environ.get("AUTH_DB_PORT") or os.environ.get("POSTGRES_HOST_PORT") or "55432",
        password=password)
    db.wait_for_database(engine)
    db.run_schema(engine, SCHEMA)
    with Session(engine) as session, session.begin():
        store.save_demo_users(session)
    app.state.engine = engine
    try:
        yield
    finally:
        engine.dispose()


app = FastAPI(title="Auth service", lifespan=lifespan)
logs.track_requests(app)


def connection(request: Request) -> Iterator[Session]:
    """A database session for one request. The endpoints only read, so nothing is committed."""
    with Session(request.app.state.engine) as session:
        yield session


DbSession = Annotated[Session, Depends(connection)]


@app.get("/health")
def health(session: DbSession) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, session: DbSession):
    account = store.find_user_by_username(session, body.username)
    # The same check and the same answer either way, so a response doesn't tell which was wrong.
    password_ok = verify_password(body.password, account.password_hash if account else DUMMY_HASH)
    if account is None or not password_ok:
        # The username only, and the same line either way, as the answer is the same.
        log.warning("login refused username=%r", logs.safe(body.username, 64))
        raise HTTPException(401, "wrong username or password")
    user = public(account)
    token = issue_token(user, request.app.state.secret, request.app.state.ttl)
    log.info("login user_id=%s role=%s", user.user_id, user.role)
    return LoginResponse(access_token=token, expires_in=request.app.state.ttl, user=user)


@app.get("/auth/me", response_model=User)
def me(request: Request, session: DbSession, authorization: Annotated[str | None, Header()] = None):
    scheme, _, token = (authorization or "").partition(" ")
    try:
        if scheme.lower() != "bearer" or not token:
            raise jwt.InvalidTokenError("not a Bearer token")
        claims = read_token(token, request.app.state.secret)
    except (jwt.InvalidTokenError, ValidationError) as error:
        why = ("no Bearer token" if scheme.lower() != "bearer" or not token
               else "an expired token" if isinstance(error, jwt.ExpiredSignatureError) else "an invalid token")
        log.warning("me refused: %s", why)      # what was wrong, never the token
        raise HTTPException(401, "log in first", headers={"WWW-Authenticate": "Bearer"}) from None
    account = store.find_user_by_id(session, claims.sub)
    if account is None:
        log.warning("me refused: user_id=%s no longer exists", claims.sub)
        raise HTTPException(401, "this user no longer exists", headers={"WWW-Authenticate": "Bearer"})
    return public(account)
