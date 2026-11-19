"""Shared test helpers: the worker fixtures under tests/fixtures/<ec>/<case_id>/."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from kyb_contracts.worker_io import Finding, WorkerInput

from app.clients.case_client import FakeCaseClient
from app.tools import ToolContext

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CASE_FOLDERS = {p.parent.name: p.parent for p in sorted(FIXTURES.rglob("case.json"))}
CASE_IDS = sorted(CASE_FOLDERS)

# Fixtures are written for a review on this day.
NOW = datetime(2026, 11, 19, 12, 0, 0)

# A full SSN, EIN, or any 9-digit run.
TIN_LIKE = re.compile(r"(?<!\d)(?:\d{3}-\d{2}-\d{4}|\d{2}-\d{7}|\d{9})(?!\d)")


def load_json(case_id: str, name: str) -> Any:
    return json.loads((CASE_FOLDERS[case_id] / name).read_text(encoding="utf-8"))


def task(case_id: str, worker: str, **changes: Any) -> WorkerInput:
    data = load_json(case_id, "worker_tasks.json")[worker]
    data.update(changes)
    return WorkerInput.model_validate(data)


def fake_client() -> FakeCaseClient:
    """All fixtures in one set of tables, like the real case service."""
    return FakeCaseClient(CASE_FOLDERS.values())


def tool_context(case_id: str, worker: str, client=None, **changes: Any) -> ToolContext:
    return ToolContext(client or fake_client(), task(case_id, worker, **changes), now=NOW)


def _matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _matches(v, actual[k]) for k, v in expected.items())
    return expected == actual


def finding_matches(expected: dict, finding: Finding) -> bool:
    """``field`` must be equal; ``value`` is a subset match for dicts; other keys must be equal."""
    if finding.field != expected["field"]:
        return False
    if "value" in expected and not _matches(expected["value"], finding.value):
        return False
    return all(getattr(finding, key) == value for key, value in expected.items() if key not in {"field", "value"})
