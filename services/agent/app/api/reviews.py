"""/reviews endpoints (HTTP layer shared with BEAC-5; the graph calls are BEAC-3).

POST /reviews                       start a review: {"case_id": ...} (fetched from the case
                                    service) or {"case": {...CaseInput...}} sent inline
GET  /reviews/{review_id}           status, and the ReviewOutput once awaiting_decision
POST /reviews/{review_id}/decision  DecisionInput; resumes the graph

Who is acting comes from the gateway, which verifies the JWT and forwards the user as
``X-User-Id`` / ``X-User-Role`` (to confirm with BEAC-5). analyst_id is never read from
the body.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from app.api.service import NotAwaitingDecision, ReviewNotFound, ReviewService
from kyb_contracts.case_input import CASE_ID
from kyb_contracts.decision import DecisionInput

router = APIRouter(prefix="/reviews", tags=["reviews"])

DECIDING_ROLES = {"analyst", "senior_analyst"}


class StartReview(BaseModel):
    case_id: Optional[str] = None
    case: Optional[dict[str, Any]] = None


def _service(request: Request) -> ReviewService:
    return request.app.state.reviews


@router.post("", status_code=202)
def start_review(body: StartReview, request: Request) -> dict[str, Any]:
    if body.case is not None:
        raw = body.case
    elif body.case_id:
        try:
            raw = request.app.state.case_client.get_case(body.case_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"could not load {body.case_id} from the case service: {exc}") from exc
    else:
        raise HTTPException(422, "send case_id or case")
    # Without a usable case_id there is nothing to queue or to report back to.
    if not isinstance(raw, dict) or not re.match(CASE_ID, str(raw.get("case_id", ""))):
        raise HTTPException(422, "the case has no valid case_id")
    review_id, created = _service(request).start(raw, background=request.app.state.background)
    return {"review_id": review_id, "created": created, **_service(request).status(review_id)}


@router.get("/{review_id}")
def get_review(review_id: str, request: Request) -> dict[str, Any]:
    try:
        return _service(request).status(review_id)
    except ReviewNotFound:
        raise HTTPException(404, "review not found")


@router.post("/{review_id}/decision")
def decide(review_id: str, body: DecisionInput, request: Request,
           x_user_id: Optional[str] = Header(default=None),
           x_user_role: Optional[str] = Header(default=None)) -> dict[str, Any]:
    if not x_user_id or (x_user_role or "").lower() not in DECIDING_ROLES:
        raise HTTPException(403, "only analysts can decide a review")
    try:
        return _service(request).decide(review_id, body, analyst_id=x_user_id)
    except ReviewNotFound:
        raise HTTPException(404, "review not found")
    except NotAwaitingDecision as exc:
        raise HTTPException(409, str(exc))


__all__ = ["router"]
