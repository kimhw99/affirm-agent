"""The gateway with fake services behind it: what gets through, and with which headers."""

import json
import time

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from app.main import Settings, create_app

SECRET = "test-secret-that-is-long-enough-for-hs256"
SETTINGS = Settings(jwt_secret=SECRET, service_token="service-token",
                    urls={"auth": "http://auth", "cases": "http://case", "metrics": "http://case",
                          "reviews": "http://agent"})


def auth(role="analyst", expires_in=600, secret=SECRET) -> dict[str, str]:
    """An Authorization header with a JWT like the auth service's."""
    now = int(time.time())
    claims = {"sub": "u_analyst_01", "role": role, "name": "Demo Analyst", "iat": now, "exp": now + expires_in}
    return {"Authorization": f"Bearer {jwt.encode(claims, secret, algorithm='HS256')}"}


@pytest.fixture
def gateway():
    """A test client for the gateway, and the requests that reached the services behind it."""
    seen: list[httpx.Request] = []

    def service(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"url": str(request.url)})

    with TestClient(create_app(SETTINGS, httpx.MockTransport(service))) as client:
        yield client, seen


def test_login_passes_through_without_a_token(gateway):
    client, seen = gateway
    body = {"username": "analyst", "password": "a password"}
    response = client.post("/auth/login", json=body, headers={"X-User-Id": "u_someone_else"})
    assert response.status_code == 200
    (request,) = seen
    assert str(request.url) == "http://auth/auth/login"
    assert json.loads(request.content) == body
    assert "x-user-id" not in request.headers


def test_reviews_get_who_is_acting_and_not_the_jwt(gateway):
    client, seen = gateway
    forged = {"X-User-Id": "u_someone_else", "X-User-Role": "senior_analyst"}
    client.post("/reviews/rv_1/decision", json={"decision": "approve"}, headers=auth() | forged)
    (request,) = seen
    assert str(request.url) == "http://agent/reviews/rv_1/decision"
    assert request.headers["x-user-id"] == "u_analyst_01"
    assert request.headers["x-user-role"] == "analyst"
    assert "authorization" not in request.headers


def test_cases_get_the_service_token_and_the_query(gateway):
    client, seen = gateway
    client.get("/cases", params={"status": "escalated"}, headers=auth("senior_analyst"))
    (request,) = seen
    assert str(request.url) == "http://case/cases?status=escalated"
    assert request.headers["authorization"] == "Bearer service-token"
    assert request.headers["x-user-role"] == "senior_analyst"


def test_metrics_go_to_the_case_service_with_the_service_token(gateway):
    client, seen = gateway
    assert client.get("/metrics/override-rate", headers=auth("senior_analyst")).status_code == 200
    (request,) = seen
    assert str(request.url) == "http://case/metrics/override-rate"
    assert request.headers["authorization"] == "Bearer service-token"
    assert request.headers["x-user-role"] == "senior_analyst"


def test_an_analyst_cant_see_the_override_rate(gateway):
    client, seen = gateway
    response = client.get("/metrics/override-rate", headers=auth("analyst"))
    assert response.status_code == 403 and "senior_analyst" in response.json()["detail"]
    assert seen == []


@pytest.mark.parametrize("role", ["analyst", "senior_analyst"])
def test_either_role_sees_the_queue_the_cases_their_decisions_and_reviews(gateway, role):
    client, seen = gateway
    paths = ["/cases?status=escalated", "/cases/case_1042", "/cases/case_1042/decisions", "/reviews/rv_1"]
    assert [client.get(path, headers=auth(role)).status_code for path in paths] == [200] * len(paths)
    assert len(seen) == len(paths)


@pytest.mark.parametrize("headers, status", [
    ({}, 401),
    ({"Authorization": "Bearer not-a-jwt"}, 401),
    (auth(expires_in=-60), 401),
    (auth(secret="another-secret-that-is-long-enough-too"), 401),
    (auth(role="admin"), 403),
])
def test_a_bad_or_missing_token_stops_at_the_gateway(gateway, headers, status):
    client, seen = gateway
    for path in ("/cases/case_1042", "/metrics/override-rate", "/reviews/rv_1"):
        assert client.get(path, headers=headers).status_code == status
    assert seen == []


def test_the_browser_cant_save_a_decision_or_reach_other_paths(gateway):
    client, seen = gateway
    assert client.post("/cases/case_1042/decisions", json={}, headers=auth()).status_code == 405
    assert client.get("/registry/search", params={"state": "IL"}, headers=auth()).status_code == 404
    assert seen == []


def test_an_unreachable_service_is_502():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with TestClient(create_app(SETTINGS, httpx.MockTransport(down))) as client:
        assert client.get("/cases/case_1042", headers=auth()).status_code == 502


def test_health_needs_no_token(gateway):
    client, seen = gateway
    assert client.get("/health").json() == {"status": "ok"}
    assert seen == []


def test_settings_need_a_long_jwt_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "change-me")
    monkeypatch.setenv("CASE_SERVICE_TOKEN", "service-token")
    with pytest.raises(RuntimeError, match="at least 32"):
        Settings.from_env()
