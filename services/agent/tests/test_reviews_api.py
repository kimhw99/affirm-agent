"""Who may start a review and decide it, by role and case status (docs/auth.md, Roles).

The API runs the real graph with scripted workers, and a fake case service that holds
case_1042 in whatever status a test sets (case_service_fake.py). The tests send
X-User-Id and X-User-Role as the gateway does after it verifies the JWT.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from case_service_fake import FakeCaseService, http_error
from conftest import NOW
from fakes import ScriptedWorker, evidence_1042, records_1042

from app.graph.llm import NullLeadLLM
from app.graph.workflow import Deps
from app.main import create_app

ANALYST = {"X-User-Id": "u_analyst_01", "X-User-Role": "analyst"}
SENIOR = {"X-User-Id": "u_senior_01", "X-User-Role": "senior_analyst"}
BOTH_ROLES = pytest.mark.parametrize("user", [ANALYST, SENIOR], ids=["analyst", "senior_analyst"])
CLOSED = pytest.mark.parametrize("status", ["approved", "denied", "docs_requested"])
STATUS_AFTER = {"approve": "approved", "request_docs": "docs_requested", "escalate": "escalated", "deny": "denied"}


@pytest.fixture
def api(case_1042):
    """A test client, the fake case service behind it, and the graph's deps (to see what ran)."""
    cases = FakeCaseService(case_1042)
    deps = Deps(ScriptedWorker("records", records_1042), ScriptedWorker("evidence", evidence_1042),
                llm=NullLeadLLM(), clock=lambda: NOW, result_sink=cases.send_decision)
    return TestClient(create_app(deps=deps, case_client=cases, background=False)), cases, deps


def start(client, user, case_id="case_1042"):
    return client.post("/reviews", json={"case_id": case_id}, headers=user)


def decide(client, review_id, decision, user):
    return client.post(f"/reviews/{review_id}/decision", json={"decision": decision, "note": "checked"},
                       headers=user)


def status_of(client, review_id):
    return client.get(f"/reviews/{review_id}").json()["status"]


# ---------- a pending_review case: either role ----------

@BOTH_ROLES
@pytest.mark.parametrize("decision", ["approve", "request_docs", "escalate", "deny"])
def test_either_role_reviews_and_decides_a_pending_case(api, user, decision):
    client, cases, _ = api
    started = start(client, user)
    assert started.status_code == 202 and started.json()["status"] == "awaiting_decision"
    done = decide(client, started.json()["review_id"], decision, user)
    assert done.status_code == 200 and done.json()["status"] == "done"
    (saved,) = cases.saved
    assert saved["case_status"] == STATUS_AFTER[decision]
    assert saved["decision_record"]["analyst_id"] == user["X-User-Id"]


# ---------- an escalated case: senior analysts only, and not escalate again ----------

def test_an_analyst_cant_review_or_decide_an_escalated_case(api):
    client, cases, deps = api
    cases.set_status("case_1042", "escalated")
    refused = start(client, ANALYST)
    assert refused.status_code == 403 and "senior_analyst" in refused.json()["detail"]
    assert deps.records_worker.calls == []                       # the graph never ran
    rid = start(client, SENIOR).json()["review_id"]
    assert decide(client, rid, "deny", ANALYST).status_code == 403
    assert status_of(client, rid) == "awaiting_decision"         # untouched, and an analyst can still read it
    assert cases.saved == []


@pytest.mark.parametrize("decision", ["approve", "request_docs", "deny"])
def test_a_senior_analyst_decides_an_escalated_case(api, decision):
    client, cases, _ = api
    cases.set_status("case_1042", "escalated")
    started = start(client, SENIOR)
    assert started.status_code == 202
    assert decide(client, started.json()["review_id"], decision, SENIOR).status_code == 200
    assert cases.saved[0]["case_status"] == STATUS_AFTER[decision]


def test_a_senior_analyst_cant_escalate_an_escalated_case_again(api):
    client, cases, _ = api
    cases.set_status("case_1042", "escalated")
    rid = start(client, SENIOR).json()["review_id"]
    refused = decide(client, rid, "escalate", SENIOR)
    assert refused.status_code == 409 and "already escalated" in refused.json()["detail"]
    assert status_of(client, rid) == "awaiting_decision"         # refused before the graph resumed
    assert cases.saved == []
    assert decide(client, rid, "approve", SENIOR).status_code == 200


# ---------- approved, denied, docs_requested: no review and no decision ----------

@BOTH_ROLES
@CLOSED
def test_a_closed_case_cant_start_a_review(api, user, status):
    client, cases, deps = api
    cases.set_status("case_1042", status)
    refused = start(client, user)
    assert refused.status_code == 409 and status in refused.json()["detail"]
    assert deps.records_worker.calls == []


@CLOSED
def test_a_case_decided_since_the_review_started_cant_take_the_decision(api, status):
    client, cases, _ = api
    rid = start(client, ANALYST).json()["review_id"]
    cases.set_status("case_1042", status)                        # e.g. another review decided it first
    for user in (ANALYST, SENIOR):
        assert decide(client, rid, "approve", user).status_code == 409
    assert status_of(client, rid) == "awaiting_decision"
    assert cases.saved == []


# ---------- what POST /reviews takes ----------

def test_a_case_sent_with_the_request_is_422(api, case_1042):
    client, _, deps = api
    for body in ({"case": case_1042}, {"case_id": "case_1042", "case": case_1042}):
        assert client.post("/reviews", json=body, headers=ANALYST).status_code == 422
    assert deps.records_worker.calls == []


def test_starting_a_review_needs_a_user_from_the_gateway(api):
    client, _, deps = api
    assert client.post("/reviews", json={"case_id": "case_1042"}).status_code == 403
    assert start(client, {"X-User-Role": "analyst"}).status_code == 403
    assert start(client, {"X-User-Id": "u_1", "X-User-Role": "admin"}).status_code == 403
    assert deps.records_worker.calls == []


def test_an_unknown_case_is_404_and_a_case_service_that_cant_answer_is_502(api):
    client, cases, _ = api
    assert start(client, ANALYST, case_id="case_9999").status_code == 404
    rid = start(client, ANALYST).json()["review_id"]

    cases.reachable = False
    assert start(client, ANALYST).status_code == 502
    assert decide(client, rid, "approve", ANALYST).status_code == 502
    assert status_of(client, rid) == "awaiting_decision"         # nothing resumed

    def broken(case_id):
        raise http_error(500, "GET", f"/cases/{case_id}", "database unavailable")
    cases.reachable, cases.get_case = True, broken
    assert start(client, ANALYST).status_code == 502
    assert cases.saved == []


def test_a_case_that_disappeared_is_404_when_deciding(api):
    client, cases, _ = api
    rid = start(client, ANALYST).json()["review_id"]
    del cases.cases["case_1042"]
    assert decide(client, rid, "approve", ANALYST).status_code == 404
    assert status_of(client, rid) == "awaiting_decision"


# ---------- the case service refuses the decision when the graph sends it (step 11) ----------

def test_a_decision_the_case_service_refuses_is_409_and_fails_the_review(api):
    client, cases, _ = api
    rid = start(client, ANALYST).json()["review_id"]
    # The case was still pending when the API checked it, but the case service says no.
    cases.decision_error = http_error(409, "POST", "/cases/case_1042/decisions",
                                      "case case_1042 is approved, so it can't take a decision")
    refused = decide(client, rid, "approve", ANALYST)
    assert refused.status_code == 409 and "can't take a decision" in refused.json()["detail"]
    review = client.get(f"/reviews/{rid}").json()
    assert review["status"] == "failed" and "can't take a decision" in review["error"]
    # Its key was released: starting again runs a new review instead of returning this one.
    cases.decision_error = None
    again = start(client, ANALYST).json()
    assert again["created"] is True and again["review_id"] != rid
    assert decide(client, again["review_id"], "approve", ANALYST).status_code == 200


def test_a_decision_that_cant_reach_the_case_service_is_502_and_fails_the_review(api):
    client, cases, _ = api
    rid = start(client, ANALYST).json()["review_id"]
    cases.decision_error = httpx.ConnectError("connection refused")
    assert decide(client, rid, "approve", ANALYST).status_code == 502
    assert status_of(client, rid) == "failed"
    assert decide(client, rid, "approve", ANALYST).status_code == 409    # a failed review takes no decision
