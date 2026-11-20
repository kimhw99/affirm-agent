"""The auth service without a database: passwords, tokens, and its two endpoints."""

import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app import main
from app.security import hash_password, issue_token, read_token, verify_password
from kyb_contracts.auth import User

SECRET = "test-secret-that-is-long-enough-for-hs256"
ANALYST = {"user_id": "u_analyst_01", "username": "analyst", "display_name": "Demo Analyst",
           "role": "analyst", "password_hash": hash_password("right password")}
USER = User(**{k: ANALYST[k] for k in ("user_id", "username", "display_name", "role")})


class FakeConnection:
    """Answers the service's queries from user rows, matched on the first parameter."""

    def __init__(self, users):
        self.users = users
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        key = params[0] if params else None
        return FakeCursor([user for user in self.users if key in (user["username"], user["user_id"])])


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return dict(self.rows[0]) if self.rows else None


@pytest.fixture
def client():
    main.app.state.secret, main.app.state.ttl = SECRET, 3600
    main.app.dependency_overrides[main.connection] = lambda: FakeConnection([ANALYST])
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


def test_demo_users_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("DEMO_ANALYST_PASSWORD", "analyst password")
    monkeypatch.delenv("DEMO_SENIOR_PASSWORD", raising=False)
    conn = FakeConnection([])
    main.save_demo_users(conn)
    assert [params[:4] for _, params in conn.statements] == [["u_analyst_01", "analyst", "Demo Analyst", "analyst"]]
    assert verify_password("analyst password", conn.statements[0][1][4])


def test_a_short_jwt_secret_is_refused(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "change-me")
    with pytest.raises(RuntimeError, match="at least 32"):
        main.jwt_secret()
