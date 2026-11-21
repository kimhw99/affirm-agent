"""/reviews endpoints (HTTP layer shared with BEAC-5; the graph calls are BEAC-3).

POST /reviews                       start a review: {"case_id": ...} and nothing else (422
                                    otherwise). The case is read from the case service, never
                                    taken from the request, so a review is always of the
                                    stored case
GET  /reviews/{review_id}           status, and the ReviewOutput once awaiting_decision
POST /reviews/{review_id}/decision  DecisionInput; resumes the graph

Who is acting comes from the gateway, which verifies the JWT and forwards the user as
``X-User-Id`` / ``X-User-Role``; analyst_id is never read from the body. Starting a review
and deciding one both check the role against the case's status, read from the case
service each time (docs/auth.md, Roles):

- ``pending_review``: an analyst or a senior analyst may review and decide it.
- ``escalated``: only a senior analyst (403 for an analyst), who can't escalate it again (409).
- any other status (approved, denied, docs_requested): no review and no decision (409).

An unknown case is 404; a case service that can't answer is 502. A decision is checked
before the graph resumes, so a refused one changes nothing. If the case service still
refuses it when the graph sends it (step 11), the review fails: 409 if the case service
answered 409, 502 for any other error.

Each review started or picked up, and each decision recorded, is an info line; each one
refused is a warning, and an error from 500 on (app/api/logs.py).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api import logs
from app.api.service import DecisionFailed, NotAwaitingDecision, ReviewNotFound, ReviewService
from kyb_contracts.auth import ROLES
from kyb_contracts.case_input import CASE_ID
from kyb_contracts.decision import DecisionInput

router = APIRouter(prefix="/reviews", tags=["reviews"])
log = logging.getLogger(__name__)

# The roles that may start a review of a case, or decide it, in each case status. A case
# in any other status can't take a decision (409), as the case service also enforces.
MAY_ACT: dict[str, frozenset[str]] = {
    "pending_review": ROLES,
    "escalated": frozenset({"senior_analyst"}),
}
# The decisions a case in that status can't take.
REFUSED_DECISIONS: dict[str, frozenset[str]] = {
    "escalated": frozenset({"escalate"}),
}


class StartReview(BaseModel):
    model_config = ConfigDict(extra="forbid")   # a "case" sent along is 422

    case_id: str = Field(pattern=CASE_ID)


def _service(request: Request) -> ReviewService:
    return request.app.state.reviews


def _refused(what: str, refused: HTTPException, about: str, user_id: Optional[str], role: Optional[str]) -> None:
    """A warning for a refused review or decision, an error from 500 on; the reason, never the body."""
    log.log(logging.ERROR if refused.status_code >= 500 else logging.WARNING,
            "%s refused %s user_id=%s role=%s status=%d: %s", what, about, logs.safe(user_id or "-", 64),
            logs.safe(role or "-", 32), refused.status_code, refused.detail)


def _role(user_id: Optional[str], role: Optional[str], action: str) -> str:
    """The role the gateway forwarded: 403 without a user, or for a role the app doesn't know."""
    role = (role or "").lower()
    if not user_id or role not in ROLES:
        raise HTTPException(403, f"only analysts can {action} a review")
    return role


def _load_case(request: Request, case_id: str) -> dict[str, Any]:
    """The case as the case service has it now: 404 if it doesn't know it, 502 if it can't say."""
    try:
        case = request.app.state.case_client.get_case(case_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(404, f"the case service has no {case_id}") from exc
        raise HTTPException(502, f"the case service answered {exc.response.status_code} for {case_id}") from exc
    except Exception as exc:  # noqa: BLE001 - unreachable, timed out, or not JSON
        raise HTTPException(502, f"could not load {case_id} from the case service: {exc}") from exc
    if not isinstance(case, dict) or case.get("case_id") != case_id:
        raise HTTPException(502, f"the case service didn't send {case_id}")
    return case


def _check(role: str, case: dict[str, Any], action: str, decision: Optional[str] = None) -> None:
    """The Roles table (docs/auth.md) for this case's status: 403 for the role, 409 for the case."""
    case_id, status = case["case_id"], case.get("status")
    allowed = MAY_ACT.get(status)
    if allowed is None:
        raise HTTPException(409, f"{case_id} is {status}, so it can't be reviewed or decided")
    if role not in allowed:
        raise HTTPException(403, f"only {' or '.join(sorted(allowed))} can {action} a case that is {status}")
    if decision in REFUSED_DECISIONS.get(status, ()):
        raise HTTPException(409, f"{case_id} is already {status}, so decide something other than {decision}")


@router.post("", status_code=202)
def start_review(body: StartReview, request: Request,
                 x_user_id: Optional[str] = Header(default=None),
                 x_user_role: Optional[str] = Header(default=None)) -> dict[str, Any]:
    try:
        role = _role(x_user_id, x_user_role, "start")
        case = _load_case(request, body.case_id)
        _check(role, case, "review")
    except HTTPException as refused:
        _refused("review", refused, f"case_id={body.case_id}", x_user_id, x_user_role)
        raise
    review_id, created = _service(request).start(case, background=request.app.state.background)
    log.info("review %s review_id=%s case_id=%s user_id=%s role=%s", "started" if created else "picked up",
             review_id, body.case_id, logs.safe(x_user_id, 64), role)
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
    try:
        role = _role(x_user_id, x_user_role, "decide")
        service = _service(request)
        try:
            current = service.status(review_id)
        except ReviewNotFound:
            raise HTTPException(404, "review not found")
        if current["status"] != "awaiting_decision":
            raise HTTPException(409, f"review {review_id} is {current['status']}")
        # The case's status now, not when the review started: it may have been decided since.
        _check(role, _load_case(request, current["case_id"]), "decide", body.decision)
        try:
            result = service.decide(review_id, body, analyst_id=x_user_id)
        except ReviewNotFound:
            raise HTTPException(404, "review not found")
        except NotAwaitingDecision as exc:
            raise HTTPException(409, str(exc))
        except DecisionFailed as exc:
            raise HTTPException(409 if exc.status_code == 409 else 502, str(exc))
    except HTTPException as refused:
        _refused("decision", refused, f"review_id={logs.safe(review_id, 64)} decision={body.decision}",
                 x_user_id, x_user_role)
        raise
    log.info("decision recorded review_id=%s case_id=%s decision=%s user_id=%s role=%s",
             review_id, current["case_id"], body.decision, logs.safe(x_user_id, 64), role)
    return result


__all__ = ["router"]
