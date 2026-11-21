"""A stand-in for the case service as the /reviews API sees it (``CaseServiceClient``).

``get_case`` serves the cases it holds, in whatever status a test sets, and fails the way
the real client does: ``httpx.HTTPStatusError`` for an answer like 404, ``httpx.ConnectError``
when the service is down. ``send_decision`` saves what the graph sends in step 11, and
moves the case to its new status, as the real case service does, unless a test sets
``decision_error``.
"""

from __future__ import annotations

import copy
from typing import Any, Optional

import httpx


def http_error(status: int, method: str, path: str, detail: str) -> httpx.HTTPStatusError:
    """What ``CaseServiceClient`` raises when the case service answers ``status``."""
    request = httpx.Request(method, f"http://case:8000{path}")
    response = httpx.Response(status, json={"detail": detail}, request=request)
    return httpx.HTTPStatusError(f"{status} for {method} {path}", request=request, response=response)


class FakeCaseService:
    def __init__(self, *cases: dict[str, Any]):
        self.cases = {case["case_id"]: case for case in cases}
        self.saved: list[dict[str, Any]] = []
        self.reachable = True
        self.decision_error: Optional[Exception] = None

    def set_status(self, case_id: str, status: str) -> None:
        self.cases[case_id]["status"] = status

    def get_case(self, case_id: str) -> dict[str, Any]:
        if not self.reachable:
            raise httpx.ConnectError("connection refused")
        if case_id not in self.cases:
            raise http_error(404, "GET", f"/cases/{case_id}", "case not found")
        return copy.deepcopy(self.cases[case_id])

    def send_decision(self, payload: dict[str, Any]) -> None:
        if self.decision_error is not None:
            raise self.decision_error
        self.saved.append(payload)
        self.set_status(payload["decision_record"]["case_id"], payload["case_status"])
