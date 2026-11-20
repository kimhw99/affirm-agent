"""HTTP client for the case service (BEAC-5): fetch a case, send the final decision back.

``GET /cases/{case_id}`` is agreed (docs/case-service-endpoints.md). The decision
endpoint is still to confirm with the case-service owner; it is
``POST /cases/{case_id}/decisions`` here, with ``{decision_record, case_status}``.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx


class CaseServiceClient:
    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None, timeout: float = 10.0):
        self.base_url = (base_url or os.getenv("CASE_SERVICE_URL", "http://case:8000")).rstrip("/")
        self.token = token or os.getenv("CASE_SERVICE_TOKEN")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def get_case(self, case_id: str) -> dict[str, Any]:
        r = httpx.get(f"{self.base_url}/cases/{case_id}", headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def send_decision(self, payload: dict[str, Any]) -> None:
        """Step 11: hand the accepted/rejected result back to the backend that sent the case."""
        case_id = payload["decision_record"]["case_id"]
        r = httpx.post(f"{self.base_url}/cases/{case_id}/decisions", json=payload,
                       headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()


__all__ = ["CaseServiceClient"]
