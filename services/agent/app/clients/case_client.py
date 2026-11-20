"""Read access to the case service: the 10 endpoints in docs/case-service-endpoints.md.

Two implementations of one ``CaseDataClient`` protocol:

- ``HttpCaseClient`` calls the real case service (``CASE_SERVICE_URL``, default
  ``DEFAULT_CASE_SERVICE_URL``; Bearer ``CASE_SERVICE_TOKEN``). The lead's
  ``CaseServiceClient`` uses the same default.
- ``FakeCaseClient`` reads golden-case folders (``case.json`` + ``world.json``) from
  disk, so the offline tests see the same data the case service seeds from. Other
  files in a case folder (``worker_tasks.json``, ``expected_findings.json``) are ignored.

Response models come from ``kyb_contracts`` and are never redefined here.
Not found: a path endpoint returns ``None``; a search returns ``[]``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError
from rapidfuzz import fuzz

from kyb_contracts.case_input import BankVerification, CaseInput, Document, OwnerKYC, WebSnapshot
from kyb_contracts.world import CommercialProfile, DbaFiling, Filing, RegistryEntity, TinRecord, World

M = TypeVar("M", bound=BaseModel)

SEARCH_LIMIT = 20

# The case service's address inside Docker Compose (docker-compose.yml, .env.example).
DEFAULT_CASE_SERVICE_URL = "http://case:8000"


class CaseServiceError(RuntimeError):
    """The case service failed, refused the call, or returned a shape we can't read."""


class CaseDataClient(Protocol):
    # Records worker: official and third-party data (endpoints 1-7)
    def search_registry(self, state: str, name: Optional[str] = None, officer: Optional[str] = None,
                        address: Optional[str] = None) -> list[RegistryEntity]: ...
    def get_registry_entity(self, state: str, state_entity_id: str) -> Optional[RegistryEntity]: ...
    def get_filings(self, state: str, state_entity_id: str) -> Optional[list[Filing]]: ...
    def search_dba(self, state: str, name: str, county: Optional[str] = None) -> list[DbaFiling]: ...
    def search_commercial(self, state: str, name: Optional[str] = None,
                          address: Optional[str] = None) -> list[CommercialProfile]: ...
    def get_tin_record(self, case_id: str) -> Optional[TinRecord]: ...
    def get_owner_kyc(self, case_id: str, kyc_id: str) -> Optional[OwnerKYC]: ...
    # Evidence worker: merchant evidence (endpoints 8-10)
    def get_document(self, case_id: str, doc_id: str) -> Optional[Document]: ...
    def get_bank_verification(self, case_id: str, bank_id: str) -> Optional[BankVerification]: ...
    def get_web_snapshot(self, case_id: str, web_id: str) -> Optional[WebSnapshot]: ...


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------

def _seg(value: str) -> str:
    return quote(value, safe="")


class HttpCaseClient:
    """Calls the case service. One retry on a 5xx or a network error; 404 is "not found"."""

    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None,
                 timeout: float = 5.0, transport: Optional[httpx.BaseTransport] = None):
        base_url = base_url or os.environ.get("CASE_SERVICE_URL") or DEFAULT_CASE_SERVICE_URL
        token = token if token is not None else os.environ.get("CASE_SERVICE_TOKEN", "")
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers,
                                  timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "HttpCaseClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- plumbing ----

    def _get_json(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        """The decoded JSON body, or ``None`` on 404."""
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        last_error: Optional[Exception] = None
        for _attempt in range(2):
            try:
                response = self._http.get(path, params=params)
            except httpx.HTTPError as error:  # network error, timeout
                last_error = error
                continue
            if response.status_code >= 500:
                last_error = CaseServiceError(f"GET {path}: HTTP {response.status_code}")
                continue
            if response.status_code == 404:
                return None
            if response.status_code >= 400:
                raise CaseServiceError(f"GET {path}: HTTP {response.status_code}")
            try:
                return response.json()
            except ValueError as error:
                raise CaseServiceError(f"GET {path}: the response is not JSON") from error
        raise CaseServiceError(f"GET {path} failed after a retry: {last_error}") from last_error

    @staticmethod
    def _parse(path: str, data: Any, model: type[M]) -> M:
        try:
            return model.model_validate(data)
        except ValidationError as error:
            raise CaseServiceError(f"GET {path}: unexpected response shape "
                                   f"({error.error_count()} problem(s))") from None

    @staticmethod
    def _parse_list(path: str, data: Any, model: type[M]) -> list[M]:
        try:
            return TypeAdapter(list[model]).validate_python(data)
        except ValidationError as error:
            raise CaseServiceError(f"GET {path}: unexpected response shape "
                                   f"({error.error_count()} problem(s))") from None

    def _one(self, path: str, model: type[M]) -> Optional[M]:
        data = self._get_json(path)
        return None if data is None else self._parse(path, data, model)

    def _many(self, path: str, model: type[M], params: Optional[dict[str, Any]] = None) -> list[M]:
        data = self._get_json(path, params)
        return [] if data is None else self._parse_list(path, data, model)

    # ---- endpoints 1-10 ----

    def search_registry(self, state, name=None, officer=None, address=None):
        return self._many("/registry/search", RegistryEntity,
                          {"state": state, "name": name, "officer": officer, "address": address})

    def get_registry_entity(self, state, state_entity_id):
        return self._one(f"/registry/entities/{_seg(state)}/{_seg(state_entity_id)}", RegistryEntity)

    def get_filings(self, state, state_entity_id):
        path = f"/registry/entities/{_seg(state)}/{_seg(state_entity_id)}/filings"
        data = self._get_json(path)
        return None if data is None else self._parse_list(path, data, Filing)

    def search_dba(self, state, name, county=None):
        return self._many("/registry/dba", DbaFiling, {"state": state, "county": county, "name": name})

    def search_commercial(self, state, name=None, address=None):
        return self._many("/commercial/search", CommercialProfile,
                          {"state": state, "name": name, "address": address})

    def get_tin_record(self, case_id):
        return self._one(f"/cases/{_seg(case_id)}/tin-record", TinRecord)

    def get_owner_kyc(self, case_id, kyc_id):
        return self._one(f"/cases/{_seg(case_id)}/owner-kyc/{_seg(kyc_id)}", OwnerKYC)

    def get_document(self, case_id, doc_id):
        return self._one(f"/cases/{_seg(case_id)}/documents/{_seg(doc_id)}", Document)

    def get_bank_verification(self, case_id, bank_id):
        return self._one(f"/cases/{_seg(case_id)}/bank-verification/{_seg(bank_id)}", BankVerification)

    def get_web_snapshot(self, case_id, web_id):
        return self._one(f"/cases/{_seg(case_id)}/web-snapshots/{_seg(web_id)}", WebSnapshot)


# --------------------------------------------------------------------------
# Fake client over golden-case folders
# --------------------------------------------------------------------------

# Words too common to search on: entity designators, joiners.
_STOPWORDS = {
    "the", "and", "of", "llc", "lc", "pllc", "inc", "incorporated", "corp", "corporation",
    "co", "company", "ltd", "limited", "llp", "lp", "pc", "dba",
}
_FUZZY_WORD = 80.0


def _words(text: str) -> list[str]:
    return re.sub(r"[^0-9a-z]+", " ", text.casefold().replace("&", " and ")).split()


def _significant(text: str) -> list[str]:
    words = [w for w in _words(text) if w not in _STOPWORDS and len(w) > 1]
    return words or _words(text)


def broad_match(query: str, text: str) -> bool:
    """Like the case service's search: any significant query word appears in the text,
    as a substring (ILIKE) or as a close word (fuzzy word ratio, like pg_trgm)."""
    haystack = text.casefold()
    text_words = _words(text)
    for word in _significant(query):
        if word in haystack:
            return True
        if any(fuzz.ratio(word, other) >= _FUZZY_WORD for other in text_words):
            return True
    return False


def _county_key(county: str) -> str:
    return re.sub(r"\s+county$", "", county.strip(), flags=re.IGNORECASE).casefold()


class FakeCaseClient:
    """Serves golden-case folders the way the case service would.

    Every loaded ``world.json`` goes into one set of "tables", as in the real service,
    so searches see all loaded cases. Case-scoped endpoints (6-10) read only their case.
    """

    def __init__(self, folders: Iterable[Path | str]):
        self.cases: dict[str, CaseInput] = {}
        self.worlds: dict[str, World] = {}
        for folder in map(Path, folders):
            case = CaseInput.model_validate_json((folder / "case.json").read_text(encoding="utf-8"))
            self.cases[case.case_id] = case
            world_path = folder / "world.json"
            if world_path.is_file():
                world = World.model_validate_json(world_path.read_text(encoding="utf-8"))
                self.worlds[world.case_id] = world

    @classmethod
    def from_dir(cls, root: Path | str) -> "FakeCaseClient":
        """Every folder under ``root`` (at any depth) that holds a ``case.json``."""
        return cls(sorted(p.parent for p in Path(root).rglob("case.json")))

    # ---- tables ----

    def _entities(self):
        for world in self.worlds.values():
            yield from world.registry

    @staticmethod
    def _public_entity(entity) -> RegistryEntity:
        return RegistryEntity.model_validate(entity.model_dump(exclude={"filings"}))

    def _find_entity(self, state: str, state_entity_id: str):
        for entity in self._entities():
            if entity.state == state.upper() and entity.state_entity_id == state_entity_id:
                return entity
        return None

    # ---- endpoints 1-6 ----

    def search_registry(self, state, name=None, officer=None, address=None):
        if not (name or officer or address):
            raise CaseServiceError("GET /registry/search: name, officer or address is required")
        hits = []
        for entity in self._entities():
            if entity.state != state.upper():
                continue
            if name and not broad_match(name, " ".join([entity.legal_name, *entity.former_names])):
                continue
            if officer and not broad_match(officer, " ".join(o.name for o in entity.officers)):
                continue
            if address and not broad_match(address, entity.registered_address):
                continue
            hits.append(self._public_entity(entity))
        return hits[:SEARCH_LIMIT]

    def get_registry_entity(self, state, state_entity_id):
        entity = self._find_entity(state, state_entity_id)
        return None if entity is None else self._public_entity(entity)

    def get_filings(self, state, state_entity_id):
        entity = self._find_entity(state, state_entity_id)
        return None if entity is None else [Filing.model_validate(f.model_dump()) for f in entity.filings]

    def search_dba(self, state, name, county=None):
        hits = []
        for world in self.worlds.values():
            for filing in world.dba_filings:
                if filing.state != state.upper():
                    continue
                if county and (filing.county is None or _county_key(filing.county) != _county_key(county)):
                    continue
                if broad_match(name, filing.dba):
                    hits.append(filing)
        return hits[:SEARCH_LIMIT]

    def search_commercial(self, state, name=None, address=None):
        if not (name or address):
            raise CaseServiceError("GET /commercial/search: name or address is required")
        hits = []
        for world in self.worlds.values():
            for profile in world.commercial:
                if profile.state != state.upper():
                    continue
                if name and not broad_match(name, profile.business_name):
                    continue
                if address and not broad_match(address, profile.address):
                    continue
                hits.append(CommercialProfile.model_validate(profile.model_dump(exclude={"state"})))
        return hits[:SEARCH_LIMIT]

    def get_tin_record(self, case_id):
        world = self.worlds.get(case_id)
        return None if world is None else world.tin_record

    # ---- endpoints 7-10 ----

    def get_owner_kyc(self, case_id, kyc_id):
        case = self.cases.get(case_id)
        return next((k for k in case.owner_kyc if k.id == kyc_id), None) if case else None

    def get_document(self, case_id, doc_id):
        case = self.cases.get(case_id)
        return next((d for d in case.documents if d.id == doc_id), None) if case else None

    def get_bank_verification(self, case_id, bank_id):
        case = self.cases.get(case_id)
        bank = case.bank_verification if case else None
        return bank if bank is not None and bank.id == bank_id else None

    def get_web_snapshot(self, case_id, web_id):
        case = self.cases.get(case_id)
        return next((w for w in case.web_snapshot if w.id == web_id), None) if case else None


__all__ = ["CaseDataClient", "CaseServiceError", "FakeCaseClient", "HttpCaseClient", "broad_match"]
