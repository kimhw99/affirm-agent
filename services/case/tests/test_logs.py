"""The case service's logs: decisions and documents saved or refused, wrong service tokens,
and startup, each line with the request's X-Request-ID and never a token or a full TIN."""

import logging
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app import db, main, store
from kyb_contracts.case_input import Document
from kyb_contracts.decision import DecisionRecord

TOKEN = "test-token-that-stays-private"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
USER = {"X-User-Id": "u_analyst_01", "X-User-Role": "analyst"}
RECORD = {"review_id": "rv_7f3a", "case_id": "case_1042", "decision": "approve",
          "note": "Name change confirmed by the SoS amendment.", "analyst_id": "u_analyst_02",
          "agent_recommendation": "recommend_clear", "overridden": False, "decided_at": "2026-11-17T16:05:00-06:00"}
LETTER = {"type": "irs_147c", "extracted": {"name": "NORTHLINE BICYCLE WORKS LLC", "ein_last4": "4567"},
          "note": "Sent by email on Nov 20."}


class FakeSession:
    def execute(self, *args):
        return None


@pytest.fixture
def client(caplog):
    caplog.set_level(logging.INFO)
    main.app.state.token = TOKEN
    main.app.dependency_overrides[main.connection] = FakeSession
    yield TestClient(main.app)  # not used as a context manager, so startup doesn't connect
    main.app.dependency_overrides.clear()


def lines(caplog, name="app.main") -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == name]


class ScriptedSession:
    """Answers the store's reads in order (the case's status, then a saved decision, if any) and counts its writes."""

    def __init__(self, status, saved=None, doc_ids=()):
        self.status, self.saved, self.doc_ids, self.selects, self.writes = status, saved, doc_ids, 0, 0

    def begin(self):
        return nullcontext()

    def execute(self, statement, params=None):
        if not statement.is_select:
            self.writes += 1
            return None
        self.selects += 1
        if self.selects == 1:
            return SimpleNamespace(scalar_one_or_none=lambda: self.status)
        return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: self.saved))

    def scalars(self, statement):
        return iter(self.doc_ids)


@pytest.mark.parametrize("authorization, what", [(None, "missing"), ("Bearer a-wrong-token-value", "wrong"),
                                                 (f"Basic {TOKEN}", "wrong")])
def test_a_missing_or_wrong_service_token_is_a_warning_without_the_token(client, caplog, authorization, what):
    headers = {"Authorization": authorization} if authorization else {}
    assert client.get("/cases/case_1042", headers=headers).status_code == 401
    (line,) = lines(caplog)
    assert line.levelno == logging.WARNING and line.service == "case"
    assert line.getMessage() == f"service token {what} method=GET path=/cases/case_1042"
    assert TOKEN not in caplog.text and "a-wrong-token-value" not in caplog.text


def test_each_line_carries_the_request_id_from_the_header(client, caplog):
    client.get("/cases/case_1042", headers={"X-Request-ID": "req-from-gateway-1"})
    client.get("/cases/case_1042", headers={"X-Request-ID": "not valid!"})
    client.get("/cases/case_1042")
    assert [line.request_id for line in lines(caplog)] == ["req-from-gateway-1", "-", "-"]


def test_uvicorns_lines_for_a_request_carry_its_id_too(client, monkeypatch, caplog):
    """uvicorn logs its access line as the response starts, and an unhandled error once the app
    has raised: both after the app's middleware is done, in the request's own task."""
    def broken(session, case_id):
        raise RuntimeError("a bug")
    monkeypatch.setattr(store, "get_case", broken)

    async def uvicorn(scope, receive, send):
        async def send_and_log(message):
            if message["type"] == "http.response.start":
                logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "172.18.0.5:40000",
                                                         scope["method"], scope["path"], "1.1", message["status"])
            await send(message)
        try:
            await main.app(scope, receive, send_and_log)
        except RuntimeError:
            logging.getLogger("uvicorn.error").exception("Exception in ASGI application")

    server = TestClient(uvicorn)
    server.get("/cases/case_1042", headers=AUTH | {"X-Request-ID": "req-uvicorn-1"})
    server.get("/health", headers={"X-Request-ID": "req-health-1"})       # a health check: no access line
    (access,) = lines(caplog, "uvicorn.access")
    (error,) = lines(caplog, "uvicorn.error")
    assert access.request_id == error.request_id == "req-uvicorn-1"
    assert access.getMessage() == '172.18.0.5:40000 - "GET /cases/case_1042 HTTP/1.1" 500'


@pytest.mark.parametrize("error, status, reason", [
    (store.DecisionConflict("case case_1042 is approved, so it can't take a decision"), 409,
     "case case_1042 is approved, so it can't take a decision"),
    (store.CaseNotFound("case_1042"), 404, "no such case"),
])
def test_a_refused_decision_is_a_warning_with_the_status_and_reason(client, monkeypatch, caplog, error, status,
                                                                     reason):
    def refuse(session, record):
        raise error
    monkeypatch.setattr(store, "save_decision", refuse)
    response = client.post("/cases/case_1042/decisions", json={"decision_record": RECORD}, headers=AUTH)
    assert response.status_code == status
    (line,) = lines(caplog)
    assert line.levelno == logging.WARNING
    assert line.getMessage() == (f"decision refused case_id=case_1042 review_id=rv_7f3a decision=approve "
                                 f"status={status}: {reason}")


def test_a_decision_for_another_case_is_a_warning_too(client, monkeypatch, caplog):
    monkeypatch.setattr(store, "save_decision", lambda session, record: pytest.fail("saved"))
    body = {"decision_record": RECORD | {"case_id": "case_2001"}}
    assert client.post("/cases/case_1042/decisions", json=body, headers=AUTH).status_code == 422
    (line,) = lines(caplog)
    assert line.levelno == logging.WARNING and "status=422" in line.getMessage()


def test_a_saved_decision_is_an_info_line_with_the_status_it_changed(caplog):
    caplog.set_level(logging.INFO)
    session = ScriptedSession("pending_review")
    assert store.save_decision(session, DecisionRecord.model_validate(RECORD)) is True
    (line,) = lines(caplog, "app.store")
    assert line.levelno == logging.INFO
    assert line.getMessage() == ("decision saved case_id=case_1042 review_id=rv_7f3a decision=approve "
                                 "from=pending_review to=approved analyst_id=u_analyst_02")
    assert "Name change confirmed" not in caplog.text            # the analyst's note stays out


def test_a_retry_of_a_saved_decision_says_nothing_changed(caplog):
    caplog.set_level(logging.INFO)
    record = DecisionRecord.model_validate(RECORD)
    session = ScriptedSession("approved", saved=record.model_dump())
    assert store.save_decision(session, record) is False
    (line,) = lines(caplog, "app.store")
    assert line.getMessage() == "decision already saved case_id=case_1042 review_id=rv_7f3a: a retry, nothing changed"
    assert session.writes == 0


def test_an_added_document_is_an_info_line(caplog):
    caplog.set_level(logging.INFO)
    session = ScriptedSession("docs_requested", doc_ids=["D1"])
    document = Document(id="D1", type="irs_147c", extracted={"ein_last4": "4567"}, confidence=1.0)
    store.add_document(session, "case_1042", document, "by email", "u_analyst_01")
    (line,) = lines(caplog, "app.store")
    assert line.levelno == logging.INFO
    assert line.getMessage() == ("document added case_id=case_1042 doc_id=D2 type=irs_147c "
                                 "from=docs_requested to=pending_review actor_id=u_analyst_01")
    assert "4567" not in caplog.text and "by email" not in caplog.text


def test_a_refused_document_is_a_warning_with_the_reason_but_not_the_values(client, monkeypatch, caplog):
    monkeypatch.setattr(store, "add_document", lambda *args: pytest.fail("saved"))
    body = LETTER | {"extracted": {"name": "NORTHLINE", "ein_last4": "00-1234567"}}
    assert client.post("/cases/case_1042/documents", json=body, headers=AUTH | USER).status_code == 422
    (line,) = lines(caplog)
    assert line.levelno == logging.WARNING
    assert line.getMessage().startswith("document refused case_id=case_1042 type=irs_147c status=422: "
                                        "a full SSN or EIN appears")
    assert "00-1234567" not in caplog.text


def test_a_document_for_a_case_that_isnt_waiting_is_a_409_warning(client, monkeypatch, caplog):
    def refuse(*args):
        raise store.DocumentsNotRequested("case case_1042 is approved; only a docs_requested case takes new documents")
    monkeypatch.setattr(store, "add_document", refuse)
    assert client.post("/cases/case_1042/documents", json=LETTER, headers=AUTH | USER).status_code == 409
    (line,) = lines(caplog)
    assert line.levelno == logging.WARNING and "type=irs_147c status=409: case case_1042 is approved" in line.getMessage()


class Raw:
    """The driver's connection run_schema uses: takes the script and commits."""

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


def test_running_the_schema_at_startup_is_an_info_line(caplog):
    caplog.set_level(logging.INFO)
    db.run_schema(SimpleNamespace(raw_connection=Raw), main.SCHEMA)
    (line,) = lines(caplog, "app.db")
    assert (line.levelno, line.getMessage(), line.request_id) == (logging.INFO, "ran schema.sql", "-")


def test_waiting_for_the_database_is_one_warning_and_giving_up_an_error(monkeypatch, caplog):
    monkeypatch.setattr(db.time, "sleep", lambda seconds: None)

    class Engine:
        def __init__(self, fails):
            self.fails = fails

        def connect(self):
            if self.fails:
                self.fails -= 1
                raise OperationalError("SELECT 1", {}, Exception("connection refused\nand more detail"))
            return nullcontext(SimpleNamespace(execute=lambda statement: None))

    db.wait_for_database(Engine(2), timeout=60)             # fails twice, then connects
    (waiting,) = lines(caplog, "app.db")
    assert waiting.levelno == logging.WARNING
    assert waiting.getMessage() == "waiting for the database: (builtins.Exception) connection refused"

    caplog.clear()
    with pytest.raises(OperationalError):
        db.wait_for_database(Engine(99), timeout=0)
    (gave_up,) = lines(caplog, "app.db")
    assert gave_up.levelno == logging.ERROR
    assert gave_up.getMessage() == "the database is still unreachable after 0 seconds"
