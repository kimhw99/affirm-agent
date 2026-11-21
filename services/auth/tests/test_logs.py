"""The auth service's logs: logins, refusals and the demo users, never a password or a token."""

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, main, store
from app.security import hash_password, issue_token
from kyb_contracts.auth import User

SECRET = "test-secret-that-is-long-enough-for-hs256"
PASSWORD = "right password"
ANALYST = db.User(user_id="u_analyst_01", username="analyst", display_name="Demo Analyst", role="analyst",
                  password_hash=hash_password(PASSWORD))
USER = User(user_id="u_analyst_01", username="analyst", display_name="Demo Analyst", role="analyst")


class FakeSession:
    def execute(self, statement, params=None):
        pass


@pytest.fixture
def client(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(store, "find_user_by_username",
                        lambda session, username: ANALYST if username == ANALYST.username else None)
    monkeypatch.setattr(store, "find_user_by_id",
                        lambda session, user_id: ANALYST if user_id == ANALYST.user_id else None)
    main.app.state.secret, main.app.state.ttl = SECRET, 3600
    main.app.dependency_overrides[main.connection] = FakeSession
    yield TestClient(main.app)  # not used as a context manager, so startup doesn't connect
    main.app.dependency_overrides.clear()


def lines(caplog, name="app.main") -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == name]


def login(client, username="analyst", password=PASSWORD, request_id=None):
    headers = {"X-Request-ID": request_id} if request_id else {}
    return client.post("/auth/login", json={"username": username, "password": password}, headers=headers)


def test_a_login_is_an_info_line_with_the_user_and_role_and_the_request_id(client, caplog):
    token = login(client, request_id="req-login-1").json()["access_token"]
    (line,) = lines(caplog)
    assert (line.levelno, line.service, line.request_id) == (logging.INFO, "auth", "req-login-1")
    assert line.getMessage() == "login user_id=u_analyst_01 role=analyst"
    assert PASSWORD not in caplog.text and token not in caplog.text


def test_a_refused_login_says_the_same_thing_whichever_was_wrong(client, caplog):
    assert login(client, "analyst", "wrong password").status_code == 401
    assert login(client, "nobody", PASSWORD).status_code == 401
    wrong_password, unknown_user = lines(caplog)
    assert wrong_password.levelno == unknown_user.levelno == logging.WARNING
    assert wrong_password.getMessage() == "login refused username='analyst'"
    assert unknown_user.getMessage() == "login refused username='nobody'"
    assert "wrong password" not in caplog.text and PASSWORD not in caplog.text


def test_a_username_is_logged_short_and_on_one_line(client, caplog):
    login(client, "evil\nINFO forged line " + "x" * 200, "wrong password")
    (line,) = lines(caplog)
    assert "\n" not in line.getMessage() and len(line.getMessage()) < 120


@pytest.mark.parametrize("authorization, why", [
    (None, "no Bearer token"),
    ("Basic YW5hbHlzdDp4", "no Bearer token"),
    ("Bearer not-a-jwt", "an invalid token"),
    (f"Bearer {issue_token(USER, SECRET, 60, now=0)}", "an expired token"),
    (f"Bearer {issue_token(USER, 'another-secret-that-is-long-enough-too', 60)}", "an invalid token"),
])
def test_a_refused_me_is_a_warning_with_what_was_wrong_but_not_the_token(client, caplog, authorization, why):
    headers = {"Authorization": authorization} if authorization else {}
    assert client.get("/auth/me", headers=headers).status_code == 401
    (line,) = lines(caplog)
    assert line.levelno == logging.WARNING and line.getMessage() == f"me refused: {why}"
    if authorization:
        assert authorization.split()[1] not in caplog.text


def test_a_me_for_a_user_that_no_longer_exists_is_a_warning(client, caplog):
    gone = USER.model_copy(update={"user_id": "u_gone_01"})
    token = issue_token(gone, SECRET, 60)
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    (line,) = lines(caplog)
    assert line.levelno == logging.WARNING and line.getMessage() == "me refused: user_id=u_gone_01 no longer exists"
    assert token not in caplog.text


def test_a_successful_me_logs_nothing(client, caplog):
    token = login(client).json()["access_token"]
    caplog.clear()
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert lines(caplog) == []


def test_startup_says_which_demo_users_it_saved_without_their_passwords(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("DEMO_ANALYST_PASSWORD", "analyst demo password")
    monkeypatch.delenv("DEMO_SENIOR_PASSWORD", raising=False)
    store.save_demo_users(FakeSession())
    skipped, saved = (record.getMessage() for record in lines(caplog, "app.store"))
    assert skipped == "demo users without a password, so not created: senior"
    assert saved == "demo users created or updated: analyst (u_analyst_01, analyst)"
    assert "analyst demo password" not in caplog.text and "scrypt" not in caplog.text


def test_running_the_schema_is_an_info_line(caplog):
    caplog.set_level(logging.INFO)

    class Raw:
        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql):
            pass

        def commit(self):
            pass

        def close(self):
            pass

    class Engine:
        def raw_connection(self):
            return Raw()

    db.run_schema(Engine(), Path(main.SCHEMA))
    (line,) = lines(caplog, "app.db")
    assert line.levelno == logging.INFO and line.getMessage() == "ran schema.sql" and line.request_id == "-"
