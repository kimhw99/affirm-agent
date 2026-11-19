"""DecisionInput and DecisionRecord: the analyst's decision (steps 10 and 11).

DecisionInput is the body of POST /reviews/{review_id}/decision. DecisionRecord is
what the case service saves in step 11, in the same transaction as the status
change and the audit log entry. Evaluation reads DecisionRecord for the override rate.
"""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .case_input import CaseStatus, Contract

Decision = Literal["approve", "request_docs", "escalate", "deny"]

# Belongs to the memo format. Kept here until memo.py exists, then import it from there.
Recommendation = Literal["recommend_clear", "request_documents", "escalate", "insufficient_evidence"]

# Which decisions count as agreeing with each recommendation (proposal, to confirm).
AGREES_WITH: dict[str, set[str]] = {
    "recommend_clear": {"approve"},
    "request_documents": {"request_docs"},
    "escalate": {"escalate", "deny"},
    "insufficient_evidence": {"request_docs", "escalate"},
}

# The case status each decision leads to.
STATUS_AFTER: dict[str, CaseStatus] = {
    "approve": CaseStatus.APPROVED,
    "request_docs": CaseStatus.DOCS_REQUESTED,
    "escalate": CaseStatus.ESCALATED,
    "deny": CaseStatus.DENIED,
}


def is_override(recommendation: str, decision: str) -> bool:
    """True when the analyst's decision doesn't agree with the agent's recommendation."""
    return decision not in AGREES_WITH[recommendation]


class DecisionInput(Contract):
    """Request body of POST /reviews/{review_id}/decision, sent by the review app.

    Holds only what the analyst chooses; the backend fills in everything else.
    analyst_id comes from the JWT, never from this body.
    """

    decision: Decision
    note: str = Field(default="", max_length=2000)


class DecisionRecord(Contract):
    """One saved analyst decision, kept for the audit trail and the override rate."""

    review_id: str
    case_id: str
    decision: Decision
    note: str
    analyst_id: str
    agent_recommendation: Recommendation
    overridden: bool
    decided_at: datetime

    @model_validator(mode="after")
    def overridden_matches_rule(self) -> "DecisionRecord":
        if self.overridden != is_override(self.agent_recommendation, self.decision):
            raise ValueError("overridden doesn't match AGREES_WITH for this recommendation and decision")
        return self
