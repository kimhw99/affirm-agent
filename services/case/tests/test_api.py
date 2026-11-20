"""The case service's API without a database: each test replaces the store's functions.

What the SQL returns is checked by running the service against a seeded database
(docs/database.md).
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main, store
from kyb_contracts.case_input import CaseInput
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


class FakeConnection:
    def execute(self, *args):
        return None


@pytest.fixture
def client():
    main.app.state.token = TOKEN
    main.app.dependency_overrides[main.connection] = FakeConnection
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
