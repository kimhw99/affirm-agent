"""The auth service without a database: passwords, tokens, and its two endpoints.

The endpoints' tests replace the store's functions, as the case service's tests do; the
demo users' upsert is checked as the SQL it compiles to.
"""

import re
import time
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app import db, main, store
from app.security import hash_password, issue_token, read_token, verify_password
from kyb_contracts.auth import User

SECRET = "test-secret-that-is-long-enough-for-hs256"
ANALYST = db.User(user_id="u_analyst_01", username="analyst", display_name="Demo Analyst", role="analyst",
                  password_hash=hash_password("right password"))
USER = User(user_id="u_analyst_01", username="analyst", display_name="Demo Analyst", role="analyst")
SCHEMA = Path(__file__).resolve().parents[1] / "schema.sql"


class FakeSession:
    """Stands in for a SQLAlchemy Session: records the statements it's given."""

    def __init__(self):
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append(statement)


@pytest.fixture
def client(monkeypatch):
    users = [ANALYST]
    monkeypatch.setattr(store, "find_user_by_username",
                        lambda session, username: next((u for u in users if u.username == username), None))
    monkeypatch.setattr(store, "find_user_by_id",
                        lambda session, user_id: next((u for u in users if u.user_id == user_id), None))
    main.app.state.secret, main.app.state.ttl = SECRET, 3600
    main.app.dependency_overrides[main.connection] = FakeSession
    yield TestClient(main.app)  # not used as a context manager, so startup doesn't connect
    main.app.dependency_overrides.clear()


def _login(client, username="analyst", password="right password"):
    return client.post("/auth/login", json={"username": username, "password": password})


def test_a_password_matches_only_its_hash():
    stored = hash_password("right password")
    assert verify_password("right password", stored)
    assert not verify_password("wrong password", stored)
    assert not verify_password("right password", "not a hash")
    assert stored != hash_password("right password")  # a new salt every time


def test_a_token_carries_the_users_claims():
    claims = read_token(issue_token(USER, SECRET, ttl_seconds=60), SECRET)
    assert (claims.sub, claims.role, claims.name) == ("u_analyst_01", "analyst", "Demo Analyst")
    assert claims.exp - claims.iat == 60


def test_an_expired_or_forged_token_is_refused():
    expired = issue_token(USER, SECRET, ttl_seconds=60, now=int(time.time()) - 120)
    with pytest.raises(jwt.ExpiredSignatureError):
        read_token(expired, SECRET)
    with pytest.raises(jwt.InvalidSignatureError):
        read_token(issue_token(USER, SECRET, ttl_seconds=60), "another-secret-that-is-long-enough-too")


def test_login_returns_a_token_with_the_users_role(client):
    response = _login(client)
    assert response.status_code == 200
    body = response.json()
    assert body["user"] == USER.model_dump()
    assert body["expires_in"] == 3600
    assert read_token(body["access_token"], SECRET).role == "analyst"
    assert "password" not in response.text


@pytest.mark.parametrize("username, password", [("analyst", "wrong password"), ("nobody", "right password")])
def test_a_wrong_username_or_password_gets_the_same_401(client, username, password):
    response = _login(client, username, password)
    assert response.status_code == 401
    assert response.json() == {"detail": "wrong username or password"}


def test_me_returns_the_tokens_user(client):
    token = _login(client).json()["access_token"]
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == USER.model_dump()


@pytest.mark.parametrize("authorization", [None, "Bearer not-a-jwt", "Basic YW5hbHlzdDp4"])
def test_me_without_a_valid_token_is_401(client, authorization):
    headers = {"Authorization": authorization} if authorization else {}
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_me_for_a_user_that_no_longer_exists_is_401(client):
    gone = USER.model_copy(update={"user_id": "u_gone_01"})
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {issue_token(gone, SECRET, 60)}"})
    assert response.status_code == 401
    assert response.json() == {"detail": "this user no longer exists"}


def test_health_runs_a_query(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_demo_users_are_upserted_from_the_environment(monkeypatch):
    monkeypatch.setenv("DEMO_ANALYST_PASSWORD", "analyst password")
    monkeypatch.delenv("DEMO_SENIOR_PASSWORD", raising=False)
    session = FakeSession()
    store.save_demo_users(session)
    (statement,) = session.statements
    compiled = statement.compile(dialect=postgresql.dialect())
    assert str(compiled).startswith("INSERT INTO users (user_id, username, display_name, role, password_hash)")
    assert str(compiled).endswith("ON CONFLICT (user_id) DO UPDATE SET username = excluded.username, "
                                  "display_name = excluded.display_name, role = excluded.role, "
                                  "password_hash = excluded.password_hash")
    values = {re.sub(r"_m\d+$", "", key): value for key, value in compiled.params.items()}  # one row
    assert [values[k] for k in ("user_id", "username", "display_name", "role")] == [
        "u_analyst_01", "analyst", "Demo Analyst", "analyst"]
    assert verify_password("analyst password", values["password_hash"])


def test_without_demo_passwords_nothing_is_written(monkeypatch):
    monkeypatch.delenv("DEMO_ANALYST_PASSWORD", raising=False)
    monkeypatch.delenv("DEMO_SENIOR_PASSWORD", raising=False)
    session = FakeSession()
    store.save_demo_users(session)
    assert session.statements == []


def test_the_model_matches_schema_sql():
    """app/db.py's User has schema.sql's columns, types, nullability and keys (schema.sql creates the table)."""
    body = re.search(r"CREATE TABLE IF NOT EXISTS users \((.*?)\n\);", SCHEMA.read_text(encoding="utf-8"), re.S).group(1)
    types = {"text": "TEXT", "timestamptz": "TIMESTAMP WITH TIME ZONE"}
    columns = {}
    for line in body.splitlines():
        line = line.split("--")[0].strip().rstrip(",")
        if not line:
            continue
        name, rest = line.split(None, 1)
        columns[name] = (types[rest.split()[0]], "NOT NULL" not in rest and "PRIMARY KEY" not in rest,
                         "PRIMARY KEY" in rest, "UNIQUE" in rest)
    table = db.User.__table__
    dialect = postgresql.dialect()
    assert {c.name: (c.type.compile(dialect=dialect), c.nullable, c.primary_key, bool(c.unique))
            for c in table.columns} == columns


def test_a_short_jwt_secret_is_refused(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "change-me")
    with pytest.raises(RuntimeError, match="at least 32"):
        main.jwt_secret()
