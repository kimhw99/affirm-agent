"""HttpCaseClient against httpx.MockTransport, and FakeCaseClient against the fixtures."""

from __future__ import annotations

import httpx
import pytest

from helpers import CASE_FOLDERS, fake_client

from app.clients.case_client import CaseServiceError, FakeCaseClient, HttpCaseClient, broad_match

ENTITY = {"state": "IL", "state_entity_id": "IL-0458821", "legal_name": "NORTHLINE BICYCLE WORKS, L.L.C.",
          "former_names": ["HARBOR LIGHT CYCLES LLC"], "entity_type": "LLC", "entity_status": "active",
          "formation_date": "2023-05-30", "registered_address": "1450 W Fulton St, Chicago, IL 60607",
          "officers": [{"name": "Dana Park", "title": "Manager"}]}
FILING = {"event_id": "IL-0458821-2", "event_type": "name_change", "effective_date": "2025-03-12",
          "old_value": "HARBOR LIGHT CYCLES LLC", "new_value": "NORTHLINE BICYCLE WORKS LLC"}
DBA = {"filing_id": "IL-AN-2024-0115", "state": "IL", "county": None, "dba": "NORTHLINE CYCLES",
       "registrant": "HARBOR LIGHT CYCLES LLC", "registrant_type": "entity",
       "registrant_state_entity_id": "IL-0458821", "filed": "2024-01-15", "expires": "2029-01-15",
       "status": "active"}
PROFILE = {"profile_id": "CDB-88213", "business_name": "NORTHLINE CYCLES",
           "address": "1450 W Fulton St, Chicago, IL 60607", "first_seen": "2024-02-01",
           "last_updated": "2026-09-30"}
TIN = {"tin_type": "EIN", "tin_last4": "4567", "irs_status": "issued", "irs_name": "HARBOR LIGHT CYCLES LLC"}
KYC = {"id": "K1", "owner_ref": "owner:P1", "result": "pass"}
DOC = {"id": "D1", "type": "irs_cp575", "confidence": 0.96,
       "extracted": {"name": "HARBOR LIGHT CYCLES LLC", "ein_last4": "4567", "date": "2023-06-08"}}
BANK = {"id": "B1", "account_holder": "NORTHLINE BICYCLE WORKS LLC DBA NORTHLINE CYCLES",
        "status": "verified", "verified_at": "2026-11-10"}
WEB = {"id": "W1", "url": "https://northlinecycles.example/terms", "captured_at": "2026-11-10",
       "extracted": {"legal_name": "Northline Bicycle Works LLC", "dba": "Northline Cycles"}}

ROUTES = {
    "/registry/search": [ENTITY],
    "/registry/entities/IL/IL-0458821": ENTITY,
    "/registry/entities/IL/IL-0458821/filings": [FILING],
    "/registry/dba": [DBA],
    "/commercial/search": [PROFILE],
    "/cases/case_1042/tin-record": TIN,
    "/cases/case_1042/owner-kyc/K1": KYC,
    "/cases/case_1042/documents/D1": DOC,
    "/cases/case_1042/bank-verification/B1": BANK,
    "/cases/case_1042/web-snapshots/W1": WEB,
}


class Recorder:
    """A MockTransport handler that serves ROUTES (or a scripted status) and records requests."""

    def __init__(self, statuses=None, body=None):
        self.requests: list[httpx.Request] = []
        self.statuses = list(statuses or [])
        self.body = body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.statuses:
            status = self.statuses.pop(0)
            if status != 200:
                return httpx.Response(status, json={"detail": "x"})
        if self.body is not None:
            return httpx.Response(200, json=self.body)
        path = request.url.path
        if path not in ROUTES:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json=ROUTES[path])


def client(handler) -> HttpCaseClient:
    return HttpCaseClient("http://case:8000", token="tok", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("call, path, params", [
    (lambda c: c.search_registry("IL", name="Northline", officer=None), "/registry/search",
     {"state": "IL", "name": "Northline"}),
    (lambda c: c.get_registry_entity("IL", "IL-0458821"), "/registry/entities/IL/IL-0458821", {}),
    (lambda c: c.get_filings("IL", "IL-0458821"), "/registry/entities/IL/IL-0458821/filings", {}),
    (lambda c: c.search_dba("IL", "Northline Cycles", county="Cook"), "/registry/dba",
     {"state": "IL", "county": "Cook", "name": "Northline Cycles"}),
    (lambda c: c.search_commercial("IL", address="1450 W Fulton"), "/commercial/search",
     {"state": "IL", "address": "1450 W Fulton"}),
    (lambda c: c.get_tin_record("case_1042"), "/cases/case_1042/tin-record", {}),
    (lambda c: c.get_owner_kyc("case_1042", "K1"), "/cases/case_1042/owner-kyc/K1", {}),
    (lambda c: c.get_document("case_1042", "D1"), "/cases/case_1042/documents/D1", {}),
    (lambda c: c.get_bank_verification("case_1042", "B1"), "/cases/case_1042/bank-verification/B1", {}),
    (lambda c: c.get_web_snapshot("case_1042", "W1"), "/cases/case_1042/web-snapshots/W1", {}),
])
def test_each_endpoint_path_params_auth_and_shape(call, path, params):
    handler = Recorder()
    result = call(client(handler))
    request = handler.requests[0]
    assert request.method == "GET"
    assert request.url.path == path
    assert dict(request.url.params) == params          # None arguments are left out
    assert request.headers["Authorization"] == "Bearer tok"
    assert result  # parsed into the contract model(s)


def test_responses_parse_into_contract_models():
    c = client(Recorder())
    assert c.get_registry_entity("IL", "IL-0458821").legal_name == "NORTHLINE BICYCLE WORKS, L.L.C."
    assert c.get_filings("IL", "IL-0458821")[0].event_type == "name_change"
    assert c.get_tin_record("case_1042").irs_status == "issued"
    assert c.get_document("case_1042", "D1").extracted["ein_last4"] == "4567"


def test_404_is_none_for_a_path_and_empty_for_a_search():
    c = client(Recorder(statuses=[404, 404, 404]))
    assert c.get_registry_entity("IL", "IL-9") is None
    assert c.search_dba("IL", "Nothing") == []
    assert c.get_filings("IL", "IL-9") is None


def test_one_retry_on_5xx_then_success():
    handler = Recorder(statuses=[503, 200])
    assert client(handler).get_owner_kyc("case_1042", "K1").result == "pass"
    assert len(handler.requests) == 2


def test_two_5xx_raise_after_one_retry():
    handler = Recorder(statuses=[500, 502])
    with pytest.raises(CaseServiceError):
        client(handler).get_tin_record("case_1042")
    assert len(handler.requests) == 2


def test_network_error_is_retried_once():
    calls = []

    def flaky(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=KYC)

    assert client(flaky).get_owner_kyc("case_1042", "K1").id == "K1"
    assert len(calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_other_4xx_raise_without_retry(status):
    handler = Recorder(statuses=[status])
    with pytest.raises(CaseServiceError):
        client(handler).get_document("case_1042", "D1")
    assert len(handler.requests) == 1


def test_bad_shape_raises():
    bad_entity = {**ENTITY, "ssn": "x"}  # unknown field: the contract forbids extras
    with pytest.raises(CaseServiceError):
        client(Recorder(body=bad_entity)).get_registry_entity("IL", "IL-0458821")
    with pytest.raises(CaseServiceError):
        client(Recorder(body={"not": "a list"})).search_registry("IL", name="x")


def test_both_clients_share_the_default_base_url(monkeypatch):
    from app.clients.case_client import DEFAULT_CASE_SERVICE_URL
    from app.clients.case_service import CaseServiceClient

    monkeypatch.delenv("CASE_SERVICE_URL", raising=False)
    with HttpCaseClient() as worker_client:
        assert str(worker_client._http.base_url).rstrip("/") == DEFAULT_CASE_SERVICE_URL
    assert CaseServiceClient().base_url == DEFAULT_CASE_SERVICE_URL == "http://case:8000"


# ---------------------------------------------------------------- FakeCaseClient

def test_fake_loads_every_fixture():
    fake = fake_client()
    assert set(fake.cases) == set(CASE_FOLDERS)
    assert fake.get_tin_record("case_4402").tin_type == "SSN"


def test_fake_reads_only_case_and_world(tmp_path):
    import shutil

    src = CASE_FOLDERS["case_1042"]
    dst = tmp_path / "combo" / "case_1042"
    shutil.copytree(src, dst)
    (dst / "notes.json").write_text("not json at all", encoding="utf-8")
    assert FakeCaseClient.from_dir(tmp_path).get_document("case_1042", "D1").id == "D1"


def test_fake_registry_search_is_broad_and_state_scoped():
    fake = fake_client()
    names = {e.legal_name for e in fake.search_registry("OR", name="Greenleaf Organics")}
    assert names == {"GREENLEAF ORGANICS, LLC", "GREEN LEAF ORGANIC LLC"}
    assert fake.search_registry("IL", name="Greenleaf Organics") == []
    assert [e.state_entity_id for e in fake.search_registry("IL", officer="Dana Park")] == ["IL-0458821"]


def test_fake_entity_lookups_strip_world_only_fields():
    fake = fake_client()
    entity = fake.get_registry_entity("IL", "IL-0458821")
    assert not hasattr(entity, "filings")
    assert fake.get_registry_entity("IL", "IL-0000000") is None
    assert fake.get_filings("CO", "CO-20261488") == []
    assert fake.get_filings("CO", "CO-0") is None
    assert all(not hasattr(p, "state") for p in fake.search_commercial("IL", name="Northline"))


def test_fake_dba_county_filter():
    fake = fake_client()
    assert [d.filing_id for d in fake.search_dba("OH", "Bend Street Coffee")] == ["OH-MONT-2022-0388"]
    assert [d.filing_id for d in fake.search_dba("OH", "Bend Street", county="Montgomery County")] == \
        ["OH-MONT-2022-0388"]
    assert fake.search_dba("OH", "Bend Street Coffee", county="Franklin") == []


def test_fake_case_scoped_endpoints_stay_in_their_case():
    fake = fake_client()
    assert fake.get_document("case_1042", "D1").type == "irs_cp575"
    assert fake.get_document("case_1042", "D2") is None
    assert fake.get_document("case_9999", "D1") is None
    assert fake.get_bank_verification("case_1042", "B2") is None
    assert fake.get_web_snapshot("case_4402", "W1").extracted["legal_name"] == "Sunshine Cafe"
    assert fake.get_owner_kyc("case_4402", "K1").result == "pass"


@pytest.mark.parametrize("query, text, hit", [
    ("Northline Cycles", "NORTHLINE BICYCLE WORKS LLC", True),
    ("Greenleaf Organics", "GREEN LEAF ORGANIC LLC", True),   # fuzzy word
    ("Acme LLC", "Smith Holdings LLC", False),               # designators don't count
    ("Sunshine Cafe", "SUNRISE DINER", False),
])
def test_broad_match(query, text, hit):
    assert broad_match(query, text) is hit
