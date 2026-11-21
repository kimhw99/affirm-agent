"""The gateway's logs: one line per request, the request ID, and never a token."""

import logging
import re
import time
from pathlib import Path

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from app import logs
from app.main import Settings, create_app

SECRET = "test-secret-that-is-long-enough-for-hs256"
SERVICE_TOKEN = "the-case-service-token"
SETTINGS = Settings(jwt_secret=SECRET, service_token=SERVICE_TOKEN,
                    urls={"auth": "http://auth", "cases": "http://case", "metrics": "http://case",
                          "reviews": "http://agent"})
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def token(role="analyst", expires_in=600, secret=SECRET) -> str:
    now = int(time.time())
    claims = {"sub": "u_analyst_01", "role": role, "name": "Demo Analyst", "iat": now, "exp": now + expires_in}
    return jwt.encode(claims, secret, algorithm="HS256")


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


@pytest.fixture
def gateway(caplog):
    caplog.set_level(logging.INFO)
    seen: list[httpx.Request] = []

    def service(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    with TestClient(create_app(SETTINGS, httpx.MockTransport(service))) as client:
        yield client, seen, caplog


def lines(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == "app.main"]


def test_every_request_gets_an_id_that_goes_to_the_service_and_back(gateway):
    client, seen, _ = gateway
    response = client.get("/cases/case_1042", headers=bearer(token()))
    rid = response.headers["X-Request-ID"]
    assert UUID.fullmatch(rid)
    assert seen[0].headers["x-request-id"] == rid
    assert client.get("/cases/case_1042", headers=bearer(token())).headers["X-Request-ID"] != rid


@pytest.mark.parametrize("sent, kept", [
    ("browser-1a2b", True), ("a" * 64, True), ("a" * 65, False), ("has space", False), ("semi;colon", False),
    ("under_score", False),
])
def test_a_valid_request_id_from_the_client_is_kept_and_any_other_replaced(gateway, sent, kept):
    client, seen, caplog = gateway
    response = client.get("/cases/case_1042", headers=bearer(token()) | {"X-Request-ID": sent})
    rid = response.headers["X-Request-ID"]
    assert (rid == sent) is kept and (kept or UUID.fullmatch(rid))
    assert seen[0].headers["x-request-id"] == rid
    assert lines(caplog)[0].request_id == rid


def test_one_info_line_per_request_with_the_service_status_time_and_user(gateway):
    client, _, caplog = gateway
    response = client.get("/cases/case_1042", headers=bearer(token()))
    (line,) = lines(caplog)
    assert line.levelno == logging.INFO and line.service == "gateway"
    assert re.fullmatch(r"method=GET path=/cases/case_1042 upstream=case status=200 duration_ms=\d+ "
                        r"user_id=u_analyst_01 role=analyst", line.getMessage())
    assert line.request_id == response.headers["X-Request-ID"]


@pytest.mark.parametrize("headers, status, reason", [
    ({}, 401, "log in first"),
    (bearer("not-a-jwt"), 401, "invalid token"),
    (bearer(token(expires_in=-60)), 401, "the session has expired; log in again"),
    (bearer(token(role="admin")), 403, "this role can't use the review app"),
])
def test_a_refused_token_is_a_warning_with_the_reason(gateway, headers, status, reason):
    client, seen, caplog = gateway
    assert client.get("/reviews/rv_1", headers=headers).status_code == status
    (line,) = lines(caplog)
    assert line.levelno == logging.WARNING
    assert f"upstream=agent status={status}" in line.getMessage() and f"reason={reason!r}" in line.getMessage()
    assert seen == []


def test_no_line_holds_the_authorization_header_a_token_or_a_body(gateway):
    client, _, caplog = gateway
    good, expired, wrong_key = token(), token(expires_in=-60), token(secret="another-secret-that-is-long-enough-too")
    client.post("/cases/case_1042/documents", headers=bearer(good),
                json={"type": "w9", "extracted": {"tin_last4": "4567"}, "note": "body text that stays private"})
    client.get("/metrics/override-rate", headers=bearer(good))                  # 403 for an analyst
    client.get("/cases", headers=bearer(expired))
    client.get("/cases", headers=bearer(wrong_key))
    client.post("/auth/login", json={"username": "analyst", "password": "a password that stays private"})
    assert len(lines(caplog)) == 5
    text = caplog.text
    for secret in (good, expired, wrong_key, SECRET, SERVICE_TOKEN, "Bearer", "body text that stays private",
                   "a password that stays private"):
        assert secret not in text
    assert "authorization" not in text.lower()


def test_a_service_out_of_reach_is_an_error(caplog):
    caplog.set_level(logging.INFO)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with TestClient(create_app(SETTINGS, httpx.MockTransport(down))) as client:
        assert client.get("/cases/case_1042", headers=bearer(token())).status_code == 502
    (line,) = lines(caplog)
    assert line.levelno == logging.ERROR
    assert "upstream=case status=502" in line.getMessage() and "isn't reachable" in line.getMessage()


def test_health_checks_are_not_logged(gateway):
    client, _, caplog = gateway
    assert client.get("/health").headers["X-Request-ID"]
    assert lines(caplog) == []


def test_a_request_that_crashes_is_an_error_line_without_a_second_traceback(caplog):
    caplog.set_level(logging.INFO)

    def broken(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("a bug")              # not an httpx error, so nothing turns it into a 502

    app = create_app(SETTINGS, httpx.MockTransport(broken))
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/cases", headers=bearer(token()) | {"X-Request-ID": "req-crash-1"}).status_code == 500
    (line,) = lines(caplog)
    assert line.levelno == logging.ERROR and line.request_id == "req-crash-1" and line.exc_info is None
    assert "method=GET path=/cases upstream=case status=500" in line.getMessage()


def test_uvicorns_loggers_get_this_format_and_its_access_line_is_left_to_main_py():
    uvicorn = logging.getLogger("uvicorn")
    its_own = logging.StreamHandler()
    uvicorn.addHandler(its_own)                  # as uvicorn's own setup leaves it
    uvicorn.propagate = False
    logs.configure()
    assert its_own not in uvicorn.handlers and uvicorn.propagate
    assert logging.getLogger("uvicorn.error").handlers == [] and logging.getLogger("uvicorn.access").propagate
    assert logging.getLogger("uvicorn.access").getEffectiveLevel() == logging.WARNING


@pytest.mark.parametrize("path, kept", [("/health", False), ("/healthz", False), ("/cases", True),
                                        ("/health-report", True)])
def test_uvicorns_access_lines_for_health_checks_are_left_out(path, kept):
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:50000", "GET", path, "1.1", 200), None)
    assert logs.not_a_health_check(record) is kept


def test_a_line_has_the_utc_time_the_level_the_service_and_the_request_id():
    record = logging.LogRecord("app.main", logging.WARNING, __file__, 1, "hello %s", ("there",), None)
    record.service, record.request_id = "gateway", "req-1"
    line = logs.Formatter().format(record)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z WARNING gateway request_id=req-1 app\.main: hello there",
                        line)


def test_the_formatter_blanks_out_secrets_whoever_logged_them():
    formatter = logs.Formatter(secrets=["the-db-password", None, ""])
    text = (f"jwt {token()} header Bearer abc.def key sk-proj-1234567890abcdef lf pk-lf-1234-5678-90 "
            "tin 00-1234567 ssn 000-12-3456 password the-db-password")
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, text, None, None)
    record.service, record.request_id = "gateway", "0c6f2a1e-1234-4abc-8def-123456789012"
    line = formatter.format(record)
    for secret in ("eyJ", "abc.def", "sk-proj", "pk-lf", "00-1234567", "000-12-3456", "the-db-password"):
        assert secret not in line
    assert line.count("[redacted]") == 7
    assert "request_id=0c6f2a1e-1234-4abc-8def-123456789012" in line   # the request ID is left alone


@pytest.mark.parametrize("value, level", [("DEBUG", logging.DEBUG), (" warning ", logging.WARNING),
                                          ("error", logging.ERROR), (None, logging.INFO), ("loud", logging.INFO)])
def test_log_level_comes_from_the_environment(value, level):
    assert logs.level(value) == level


def _shared_part(path: Path) -> str:
    """A logs.py without the lines that differ: the first (it names the service) and the constants."""
    text = path.read_text(encoding="utf-8").split("\n", 1)[1]
    start = text.index("# ---------- What differs between the services ----------")
    end = text.index("# -------------------------------------------------------", start)
    return text[:start] + text[end:]


@pytest.mark.parametrize("copy", ["auth/app/logs.py", "case/app/logs.py", "agent/app/api/logs.py"])
def test_each_service_has_the_same_logs_module_but_for_its_constants(copy):
    services = Path(__file__).resolve().parents[2]
    assert _shared_part(services / copy) == _shared_part(services / "gateway/app/logs.py"), (
        f"services/{copy} differs from services/gateway/app/logs.py: make the same change in each copy")
