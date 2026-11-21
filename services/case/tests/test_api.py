"""The case service's API without a database: each test replaces the store's functions.

What the SQL returns is checked by running the service against a seeded database
(docs/database.md). How the searches put the query into their SQL is checked here,
with a session that only records what it's given.
"""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app import main, store
from kyb_contracts.case_input import CaseInput, Document
from kyb_contracts.decision import DecisionRecord
from kyb_contracts.world import CommercialProfile, RegistryEntity, World

EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "examples"
TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# Each endpoint, the query it gets, and the store function it should call, with its arguments.
ENDPOINTS = [
    ("/cases/case_1042", {}, "get_case", ("case_1042",)),
    ("/registry/search", {"state": "IL", "name": "Northline"}, "search_registry", ("IL", "Northline", None, None)),
    ("/registry/entities/IL/IL-0458821", {}, "get_registry_entity", ("IL", "IL-0458821")),
    ("/registry/entities/IL/IL-0458821/filings", {}, "get_filings", ("IL", "IL-0458821")),
    ("/registry/dba", {"state": "IL", "name": "Northline Cycles", "county": "Cook"}, "search_dba",
     ("IL", "Northline Cycles", "Cook")),
    ("/commercial/search", {"state": "IL", "address": "Fulton"}, "search_commercial", ("IL", None, "Fulton")),
    ("/cases/case_1042/tin-record", {}, "get_tin_record", ("case_1042",)),
    ("/cases/case_1042/owner-kyc/K1", {}, "get_owner_kyc", ("case_1042", "K1")),
    ("/cases/case_1042/documents/D1", {}, "get_document", ("case_1042", "D1")),
    ("/cases/case_1042/bank-verification/B1", {}, "get_bank_verification", ("case_1042", "B1")),
    ("/cases/case_1042/web-snapshots/W1", {}, "get_web_snapshot", ("case_1042", "W1")),
]
LOOKUPS = [endpoint for endpoint in ENDPOINTS if not endpoint[2].startswith("search_")]


class FakeSession:
    """Stands in for the request's SQLAlchemy Session; only /health uses it directly."""

    def execute(self, *args):
        return None


@pytest.fixture
def client():
    main.app.state.token = TOKEN
    main.app.dependency_overrides[main.connection] = FakeSession
    yield TestClient(main.app)  # not used as a context manager, so startup doesn't connect
    main.app.dependency_overrides.clear()


def _answers() -> dict:
    """What each store function returns in these tests: case_1042's records."""
    case = CaseInput.model_validate_json((EXAMPLES / "case_input" / "case_1042.json").read_text(encoding="utf-8"))
    world = World.model_validate_json((EXAMPLES / "world" / "case_1042.json").read_text(encoding="utf-8"))
    entity = RegistryEntity.model_validate(world.registry[0].model_dump(exclude={"filings"}))
    return {
        "get_case": case,
        "search_registry": [entity],
        "get_registry_entity": entity,
        "get_filings": world.registry[0].filings,
        "search_dba": world.dba_filings,
        "search_commercial": [CommercialProfile.model_validate(c.model_dump(exclude={"state"}))
                              for c in world.commercial],
        "get_tin_record": world.tin_record,
        "get_owner_kyc": case.owner_kyc[0],
        "get_document": case.documents[0],
        "get_bank_verification": case.bank_verification,
        "get_web_snapshot": case.web_snapshot[0],
    }


def _replace_store(monkeypatch, answers: dict) -> list:
    """Replaces each store function with one that records its call and returns the answer."""
    calls = []
    for name, answer in answers.items():
        def fake(conn, *args, name=name, answer=answer):
            calls.append((name, args))
            return answer
        monkeypatch.setattr(store, name, fake)
    return calls


def _json(value):
    if isinstance(value, list):
        return [item.model_dump(mode="json") for item in value]
    return value.model_dump(mode="json")


@pytest.mark.parametrize("path, query, function, args", ENDPOINTS)
def test_each_endpoint_returns_what_the_store_found(client, monkeypatch, path, query, function, args):
    answers = _answers()
    calls = _replace_store(monkeypatch, answers)
    response = client.get(path, params=query, headers=AUTH)
    assert response.status_code == 200
    assert calls == [(function, args)]
    assert response.json() == _json(answers[function])


@pytest.mark.parametrize("path, query, function, args", ENDPOINTS)
def test_each_endpoint_needs_the_token(client, path, query, function, args):
    assert client.get(path, params=query).status_code == 401
    assert client.get(path, params=query, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_health_needs_no_token(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path, query, function, args", LOOKUPS)
def test_a_missing_record_is_404(client, monkeypatch, path, query, function, args):
    _replace_store(monkeypatch, {name: None for name in _answers()})
    assert client.get(path, params=query, headers=AUTH).status_code == 404


@pytest.mark.parametrize("path, query", [
    ("/registry/search", {"state": "IL"}),
    ("/registry/search", {"state": "Illinois", "name": "Northline"}),
    ("/registry/dba", {"state": "IL"}),
    ("/commercial/search", {"state": "IL"}),
])
def test_a_search_without_its_criteria_is_422(client, path, query):
    assert client.get(path, params=query, headers=AUTH).status_code == 422


def test_search_words_drop_designators_and_joiners():
    assert store.search_words("Northline Bicycle Works, L.L.C.") == ["northline", "bicycle", "works"]
    assert store.search_words("The Smith & Sons Co") == ["smith", "sons"]
    assert store.search_words("LLC") == ["llc"]  # nothing else to match on, so it's kept
    assert store.search_words(None) == []


class RecordingSession:
    """A session that records each statement and its parameters, and finds nothing."""

    def __init__(self):
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((statement, params or {}))
        return self

    def mappings(self):
        return []


def test_searches_bind_the_query_instead_of_writing_it_into_the_sql():
    session = RecordingSession()
    hostile = "Zyxq'); DROP TABLE cases; --"
    assert store.search_registry(session, "IL", name=hostile, officer="Dana Park", address="Fulton") == []
    assert store.search_dba(session, "IL", hostile, county="Cook County") == []
    assert store.search_commercial(session, "IL", name=hostile, address="Fulton") == []
    assert len(session.calls) == 3
    for statement, params in session.calls:
        sql = str(statement)
        assert "zyxq" not in sql.lower() and "drop" not in sql.lower()
        # Every value is a parameter SQLAlchemy found in the text, so none was dropped or written in.
        assert set(statement.compile().params) == set(params)
        assert params["state"] == "IL" and "%zyxq%" in params.values()
    assert session.calls[1][1]["county"] == "Cook County"


def test_a_search_with_nothing_to_match_on_never_reaches_the_database():
    session = RecordingSession()
    assert store.search_registry(session, "IL", name="%_") == []
    assert store.search_commercial(session, "IL", name=None, address="--") == []
    assert session.calls == []


def test_a_search_scores_each_word_and_keeps_every_criterion():
    sql, params = store.search_sql("mock.commercial_profiles", "profile_id", "state = upper(:state)", "business_name",
                                   [("business_name ILIKE {pattern}", "Green Leaf"), ("address ILIKE {pattern}", "Ash")])
    assert params == {"c0_w0": "%green%", "c0_w1": "%leaf%", "c1_w0": "%ash%"}
    assert "(business_name ILIKE :c0_w0)::int + (business_name ILIKE :c0_w1)::int AS hits0" in sql
    assert "WHERE hits0 > 0 AND hits1 > 0 ORDER BY hits0 + hits1 DESC, business_name LIMIT 20" in sql


# ---------- POST /cases/{case_id}/decisions (step 11) ----------

DECISIONS = "/cases/case_1042/decisions"


def _record(**changes) -> dict:
    """case_1042's decision from the walkthrough (docs/workflow-handoffs.md 7.15), with changes."""
    return {"review_id": "rv_7f3a", "case_id": "case_1042", "decision": "approve",
            "note": "Name change confirmed by the SoS amendment.", "analyst_id": "u_analyst_02",
            "agent_recommendation": "recommend_clear", "overridden": False,
            "decided_at": "2026-11-17T16:05:00-06:00"} | changes


def _save(monkeypatch, outcome) -> list:
    """Replaces store.save_decision: it returns outcome, or raises it if it's an exception."""
    calls = []

    def fake(conn, record):
        calls.append(record)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(store, "save_decision", fake)
    return calls


def test_a_new_decision_is_saved_with_its_status(client, monkeypatch):
    calls = _save(monkeypatch, True)
    response = client.post(DECISIONS, json={"decision_record": _record(), "case_status": "approved"}, headers=AUTH)
    assert response.status_code == 201
    assert response.json()["case_status"] == "approved"
    assert [record.review_id for record in calls] == ["rv_7f3a"]


def test_the_case_status_is_worked_out_when_it_isnt_sent(client, monkeypatch):
    _save(monkeypatch, True)
    body = {"decision_record": _record(decision="escalate", overridden=True)}
    response = client.post(DECISIONS, json=body, headers=AUTH)
    assert response.status_code == 201
    assert response.json()["case_status"] == "escalated"


def test_the_same_decision_again_is_200(client, monkeypatch):
    _save(monkeypatch, False)
    assert client.post(DECISIONS, json={"decision_record": _record()}, headers=AUTH).status_code == 200


@pytest.mark.parametrize("error, status", [
    (store.CaseNotFound("case_1042"), 404),
    (store.DecisionConflict("case case_1042 is approved, so it can't take a decision"), 409),
])
def test_the_store_refusing_is_404_or_409(client, monkeypatch, error, status):
    _save(monkeypatch, error)
    response = client.post(DECISIONS, json={"decision_record": _record()}, headers=AUTH)
    assert response.status_code == status


@pytest.mark.parametrize("body", [
    {"decision_record": _record(case_id="case_2001")},        # not the path's case
    {"decision_record": _record(), "case_status": "denied"},  # approve leads to approved
    {"decision_record": _record(overridden=True)},            # approve agrees with recommend_clear
    {"decision_record": _record(), "note": "misplaced"},      # unknown field
])
def test_a_wrong_body_is_422_and_saves_nothing(client, monkeypatch, body):
    calls = _save(monkeypatch, True)
    assert client.post(DECISIONS, json=body, headers=AUTH).status_code == 422
    assert calls == []


def test_saving_needs_the_token(client, monkeypatch):
    calls = _save(monkeypatch, True)
    assert client.post(DECISIONS, json={"decision_record": _record()}).status_code == 401
    assert calls == []


def test_a_time_without_a_time_zone_counts_as_utc():
    utc = DecisionRecord.model_validate(_record(decided_at="2026-11-17T22:05:00"))
    chicago = DecisionRecord.model_validate(_record())
    assert store.same_decision(utc, chicago)
    assert not store.same_decision(utc, DecisionRecord.model_validate(_record(note="Another note.")))


# ---------- POST /cases/{case_id}/documents (the merchant's new documents) ----------

ADD = "/cases/case_1042/documents"
USER = {"X-User-Id": "u_analyst_01", "X-User-Role": "analyst"}   # as the gateway forwards them
LETTER = {"type": "irs_147c", "extracted": {"name": "NORTHLINE BICYCLE WORKS LLC", "ein_last4": "4567"},
          "note": "Sent by email on Nov 20."}


def _add(monkeypatch, outcome) -> list:
    """Replaces store.add_document: it returns the document under outcome's ID, or raises outcome."""
    calls = []

    def fake(session, case_id, document, note, actor_id):
        calls.append((case_id, document, note, actor_id))
        if isinstance(outcome, Exception):
            raise outcome
        return document.model_copy(update={"id": outcome})
    monkeypatch.setattr(store, "add_document", fake)
    return calls


def test_a_new_document_is_saved_and_the_case_goes_back_to_review(client, monkeypatch):
    calls = _add(monkeypatch, "D2")
    response = client.post(ADD, json=LETTER, headers=AUTH | USER)
    assert response.status_code == 201
    assert response.json() == {"document": {"id": "D2", "type": "irs_147c", "extracted": LETTER["extracted"],
                                            "confidence": 1.0},
                               "case_status": "pending_review"}
    ((case_id, document, note, actor),) = calls
    assert (case_id, document.type, document.confidence, note, actor) == (
        "case_1042", "irs_147c", 1.0, "Sent by email on Nov 20.", "u_analyst_01")


def test_values_are_trimmed_and_the_note_is_optional(client, monkeypatch):
    calls = _add(monkeypatch, "D2")
    body = {"type": "w9", "extracted": {"legal_name": "  Northline Bicycle Works LLC ", "tin_last4": "4567"}}
    assert client.post(ADD, json=body, headers=AUTH | USER).status_code == 201
    assert calls[0][1].extracted["legal_name"] == "Northline Bicycle Works LLC" and calls[0][2] == ""


@pytest.mark.parametrize("error, status", [
    (store.DocumentsNotRequested("case case_1042 is approved; only a docs_requested case takes new documents"), 409),
    (store.CaseNotFound("case_1042"), 404),
])
def test_the_store_refusing_is_409_or_404(client, monkeypatch, error, status):
    _add(monkeypatch, error)
    response = client.post(ADD, json=LETTER, headers=AUTH | USER)
    assert response.status_code == status


@pytest.mark.parametrize("change, message", [
    ({"extracted": {"name": "NORTHLINE", "routing_number": "x"}}, "unknown extracted fields: ['routing_number']"),
    ({"type": "bank_statement"}, "type: Input should be 'irs_cp575'"),
    ({"extracted": {"name": "NORTHLINE", "ein_last4": "  "}}, "empty fields: ein_last4"),
    ({"extracted": {"name": "NORTHLINE", "ein_last4": "00-1234567"}}, "a full SSN or EIN appears"),
    ({"note": "The SSN is 000-12-3456"}, "a full SSN or EIN appears"),
])
def test_a_document_the_contract_refuses_is_422_and_saves_nothing(client, monkeypatch, change, message):
    calls = _add(monkeypatch, "D2")
    response = client.post(ADD, json=LETTER | change, headers=AUTH | USER)
    assert response.status_code == 422 and message in response.json()["detail"]
    assert "00-1234567" not in response.text and "000-12-3456" not in response.text
    assert calls == []


@pytest.mark.parametrize("headers", [{}, {"X-User-Id": "u_analyst_01"}, {"X-User-Id": "u_1", "X-User-Role": "admin"}])
def test_adding_a_document_needs_an_analyst_from_the_gateway(client, monkeypatch, headers):
    calls = _add(monkeypatch, "D2")
    assert client.post(ADD, json=LETTER, headers=AUTH | headers).status_code == 403
    assert client.post(ADD, json=LETTER, headers=USER).status_code == 401     # and the service token
    assert calls == []


@pytest.mark.parametrize("ids, expected", [([], "D1"), (["D1"], "D2"), (["D1", "D9"], "D10"),
                                           (["D10", "D9", "D2"], "D11")])
def test_the_next_document_id_goes_by_number(ids, expected):
    assert store.next_document_id(ids) == expected


class ScriptedSession:
    """Answers add_document's reads (the case's status, its document IDs) and records its writes."""

    def __init__(self, status, doc_ids):
        self.status, self.doc_ids, self.reads, self.writes = status, doc_ids, [], []

    def begin(self):
        return nullcontext()

    def execute(self, statement, params=None):
        if statement.is_select:
            self.reads.append(statement)
            return SimpleNamespace(scalar_one_or_none=lambda: self.status)
        self.writes.append(statement)

    def scalars(self, statement):
        return iter(self.doc_ids)


def _sql(statement) -> tuple[str, dict]:
    compiled = statement.compile(dialect=postgresql.dialect())
    return str(compiled), compiled.params


def test_add_document_locks_the_case_numbers_the_document_and_reopens_the_case():
    session = ScriptedSession("docs_requested", [f"D{i}" for i in range(1, 10)])
    document = Document(id="D1", type="irs_147c", extracted={"ein_last4": "4567"}, confidence=1.0)
    saved = store.add_document(session, "case_1042", document, "by email", "u_analyst_01")
    assert saved.id == "D10" and saved.extracted == {"ein_last4": "4567"}
    assert "FOR UPDATE" in _sql(session.reads[0])[0]
    (insert_sql, inserted), (update_sql, updated), (audit_sql, audit) = map(_sql, session.writes)
    assert insert_sql.startswith("INSERT INTO documents") and inserted == {
        "case_id": "case_1042", "doc_id": "D10", "type": "irs_147c", "extracted": {"ein_last4": "4567"},
        "confidence": 1.0}
    assert update_sql.startswith("UPDATE cases SET status=") and "updated_at=now()" in update_sql
    assert updated["status"] == "pending_review"
    assert audit_sql.startswith("INSERT INTO audit_log") and {k: audit[k] for k in (
        "actor_id", "action", "from_status", "to_status", "detail")} == {
        "actor_id": "u_analyst_01", "action": "documents_received", "from_status": "docs_requested",
        "to_status": "pending_review", "detail": {"doc_id": "D10", "type": "irs_147c", "note": "by email"}}


@pytest.mark.parametrize("status, error", [("pending_review", store.DocumentsNotRequested),
                                           ("approved", store.DocumentsNotRequested), (None, store.CaseNotFound)])
def test_add_document_writes_nothing_unless_the_case_waits_for_documents(status, error):
    session = ScriptedSession(status, ["D1"])
    document = Document(id="D1", type="w9", extracted={"tin_last4": "4567"}, confidence=1.0)
    with pytest.raises(error):
        store.add_document(session, "case_1042", document, "", "u_analyst_01")
    assert session.writes == []


# ---------- For the review app ----------

SUMMARY = {"case_id": "case_1042", "status": "pending_review", "legal_name": "NORTHLINE BICYCLE WORKS LLC",
           "dba": "Northline Cycles", "state": "IL", "flag_reasons": ["NAME_SIMILAR_MATCH", "TIN_MISMATCH"],
           "created_at": "2026-11-19T10:00:00+00:00", "updated_at": "2026-11-19T10:00:00+00:00"}


def test_the_queue_lists_the_cases_with_a_status(client, monkeypatch):
    calls = []
    monkeypatch.setattr(store, "list_cases", lambda conn, status=None: calls.append(status) or [SUMMARY])
    response = client.get("/cases", params={"status": "pending_review"}, headers=AUTH)
    assert response.status_code == 200
    assert [row["case_id"] for row in response.json()] == ["case_1042"]
    assert client.get("/cases", headers=AUTH).status_code == 200
    assert calls == ["pending_review", None]


def test_the_queue_refuses_an_unknown_status(client):
    assert client.get("/cases", params={"status": "open"}, headers=AUTH).status_code == 422


def test_a_cases_decisions(client, monkeypatch):
    record = DecisionRecord.model_validate(_record())
    monkeypatch.setattr(store, "list_decisions",
                        lambda conn, case_id: [record] if case_id == "case_1042" else None)
    response = client.get("/cases/case_1042/decisions", headers=AUTH)
    assert response.status_code == 200
    assert [row["review_id"] for row in response.json()] == ["rv_7f3a"]
    assert client.get("/cases/case_9999/decisions", headers=AUTH).status_code == 404


def test_the_override_rate_adds_up_the_recommendations(client, monkeypatch):
    monkeypatch.setattr(store, "override_counts", lambda conn: [
        {"recommendation": "escalate", "decisions": 1, "overridden": 0},
        {"recommendation": "recommend_clear", "decisions": 3, "overridden": 1}])
    body = client.get("/metrics/override-rate", headers=AUTH).json()
    assert (body["decisions"], body["overridden"], body["rate"]) == (4, 1, 0.25)
    assert [group["rate"] for group in body["by_recommendation"]] == [0.0, 0.3333]


def test_without_decisions_there_is_no_override_rate_yet(client, monkeypatch):
    monkeypatch.setattr(store, "override_counts", lambda conn: [])
    assert client.get("/metrics/override-rate", headers=AUTH).json() == {
        "decisions": 0, "overridden": 0, "rate": None, "by_recommendation": []}


@pytest.mark.parametrize("path", ["/cases", "/cases/case_1042/decisions", "/metrics/override-rate"])
def test_the_review_app_endpoints_need_the_token(client, path):
    assert client.get(path).status_code == 401
