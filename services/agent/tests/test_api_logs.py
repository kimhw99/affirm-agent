"""The /reviews API's logs (app/api/logs.py): each review and decision, and each one refused,
with the request's X-Request-ID, which also goes on to the case service and into a review
running in the background."""

import logging
import re
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from case_service_fake import FakeCaseService, http_error
from conftest import NOW
from fakes import ScriptedWorker, evidence_1042, records_1042

from app.api import logs
from app.api.service import ReviewService
from app.graph.llm import NullLeadLLM
from app.graph.workflow import Deps
from app.main import RequestIdCaseServiceClient, create_app

ANALYST = {"X-User-Id": "u_analyst_01", "X-User-Role": "analyst"}
SENIOR = {"X-User-Id": "u_senior_01", "X-User-Role": "senior_analyst"}


def deps_for(sink) -> Deps:
    return Deps(ScriptedWorker("records", records_1042), ScriptedWorker("evidence", evidence_1042),
                llm=NullLeadLLM(), clock=lambda: NOW, result_sink=sink)


@pytest.fixture
def api(case_1042, caplog):
    caplog.set_level(logging.INFO)
    cases = FakeCaseService(case_1042)
    return TestClient(create_app(deps=deps_for(cases.send_decision), case_client=cases, background=False)), cases


def lines(caplog, name="app.api.reviews") -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == name]


def start(client, user=ANALYST, request_id=None):
    headers = user | ({"X-Request-ID": request_id} if request_id else {})
    return client.post("/reviews", json={"case_id": "case_1042"}, headers=headers)


def test_a_review_started_or_picked_up_is_an_info_line_with_the_request_id(api, caplog):
    client, _ = api
    rid = start(client, request_id="req-review-1").json()["review_id"]
    start(client, request_id="req-review-2")
    started, picked_up = lines(caplog)
    assert (started.levelno, started.service, started.request_id) == (logging.INFO, "agent", "req-review-1")
    assert started.getMessage() == f"review started review_id={rid} case_id=case_1042 user_id=u_analyst_01 role=analyst"
    assert picked_up.getMessage().startswith(f"review picked up review_id={rid} ")
    assert picked_up.request_id == "req-review-2"


def test_a_decision_recorded_is_an_info_line_without_the_note(api, caplog):
    client, _ = api
    rid = start(client).json()["review_id"]
    caplog.clear()
    response = client.post(f"/reviews/{rid}/decision", json={"decision": "approve", "note": "a note that stays private"},
                           headers=ANALYST | {"X-Request-ID": "req-decide-1"})
    assert response.status_code == 200
    (line,) = lines(caplog)
    assert line.levelno == logging.INFO and line.request_id == "req-decide-1"
    assert line.getMessage() == (f"decision recorded review_id={rid} case_id=case_1042 decision=approve "
                                 "user_id=u_analyst_01 role=analyst")
    assert "a note that stays private" not in caplog.text


def test_a_refused_review_is_a_warning_and_a_case_service_out_of_reach_an_error(api, caplog):
    client, cases = api
    cases.set_status("case_1042", "escalated")
    assert start(client).status_code == 403
    cases.reachable = False
    assert start(client, SENIOR).status_code == 502
    refused, unreachable = lines(caplog)
    assert refused.levelno == logging.WARNING
    assert refused.getMessage() == ("review refused case_id=case_1042 user_id=u_analyst_01 role=analyst status=403: "
                                    "only senior_analyst can review a case that is escalated")
    assert unreachable.levelno == logging.ERROR
    assert "user_id=u_senior_01 role=senior_analyst status=502: could not load case_1042" in unreachable.getMessage()


def test_a_decision_the_case_service_refuses_is_a_warning(api, caplog):
    client, cases = api
    rid = start(client).json()["review_id"]
    cases.decision_error = http_error(409, "POST", "/cases/case_1042/decisions",
                                      "case case_1042 is approved, so it can't take a decision")
    assert client.post(f"/reviews/{rid}/decision", json={"decision": "approve"}, headers=ANALYST).status_code == 409
    refused = lines(caplog)[-1]
    assert refused.levelno == logging.WARNING
    assert refused.getMessage().startswith(f"decision refused review_id={rid} decision=approve user_id=u_analyst_01 "
                                           "role=analyst status=409: the case service refused the decision (409)")


def test_a_request_without_a_user_is_logged_with_dashes(api, caplog):
    client, _ = api
    assert client.post("/reviews", json={"case_id": "case_1042"}).status_code == 403
    (line,) = lines(caplog)
    assert "user_id=- role=- status=403" in line.getMessage()


def test_the_case_service_gets_the_request_id_with_each_call(case_1042, monkeypatch, caplog):
    """Through the real client: the case is read, and the decision sent, with the request's ID."""
    sent: list[tuple[str, httpx.Headers]] = []

    def answer(method, status, body):
        def call(url, headers=None, json=None, timeout=None):
            sent.append((method, httpx.Headers(headers)))
            return httpx.Response(status, json=body, request=httpx.Request(method, url))
        return call
    monkeypatch.setattr(httpx, "get", answer("GET", 200, case_1042))
    monkeypatch.setattr(httpx, "post", answer("POST", 201, {}))
    case_client = RequestIdCaseServiceClient(base_url="http://case:8000", token="svc-token")
    client = TestClient(create_app(deps=deps_for(case_client.send_decision), case_client=case_client,
                                   background=False))

    rid = start(client, request_id="req-start-1").json()["review_id"]
    client.post(f"/reviews/{rid}/decision", json={"decision": "approve"},
                headers=ANALYST | {"X-Request-ID": "req-decide-2"})
    assert [(method, headers.get("x-request-id")) for method, headers in sent] == [
        ("GET", "req-start-1"), ("GET", "req-decide-2"), ("POST", "req-decide-2")]
    assert all(headers["authorization"] == "Bearer svc-token" for _, headers in sent)


def test_outside_a_request_no_id_is_sent():
    assert RequestIdCaseServiceClient(base_url="http://case:8000", token="svc-token")._headers() == {
        "Authorization": "Bearer svc-token"}


class Graph:
    """Runs a review in the background: notes the request ID it sees, then fails if asked to."""

    def __init__(self, fail=False):
        self.fail, self.seen, self.done = fail, None, threading.Event()

    def invoke(self, state, config):
        self.seen = logs.request_id.get()
        self.done.set()
        if self.fail:
            raise RuntimeError("the graph broke")


@pytest.mark.parametrize("fail", [False, True])
def test_a_review_in_the_background_keeps_the_request_id(caplog, fail):
    graph = Graph(fail)
    service = ReviewService(graph)
    before = set(threading.enumerate())
    token = logs.request_id.set("req-background-1")
    try:
        service.start({"case_id": "case_1042"}, background=True)
    finally:
        logs.request_id.reset(token)
    for thread in set(threading.enumerate()) - before:      # the review's thread
        thread.join(5)
    assert graph.done.is_set() and graph.seen == "req-background-1"
    if fail:
        (line,) = lines(caplog, "app.api.service")
        assert line.levelno == logging.ERROR and line.request_id == "req-background-1"


def test_the_agents_lines_blank_out_api_keys_and_tins():
    formatter = logs.Formatter(secrets=["lf-secret-value-1234"])
    record = logging.LogRecord("openai", logging.ERROR, __file__, 1,
                               "Incorrect API key provided: sk-proj-abcdefgh12345678; lf-secret-value-1234; "
                               "EIN 00-1234567", None, None)
    record.service, record.request_id = "agent", "req-1"
    line = formatter.format(record)
    assert re.search(r"Z ERROR agent request_id=req-1 openai: ", line)
    for secret in ("sk-proj", "lf-secret-value-1234", "00-1234567"):
        assert secret not in line
