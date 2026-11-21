// Labels and rules the pages share.

export const CASE_STATUSES = ["pending_review", "escalated", "docs_requested", "approved", "denied"];

export const DECISIONS = [
  { value: "approve", label: "Approve" },
  { value: "request_docs", label: "Request documents" },
  { value: "escalate", label: "Escalate" },
  { value: "deny", label: "Deny" },
];

// Which decisions agree with each recommendation. Mirrors AGREES_WITH in
// contracts/kyb_contracts/decision.py, which decides `overridden` when the decision is saved.
export const AGREES_WITH = {
  recommend_clear: ["approve"],
  request_documents: ["request_docs"],
  escalate: ["escalate", "deny"],
  insufficient_evidence: ["request_docs", "escalate"],
};

// The queue each role starts on.
export function defaultQueue(role) {
  return role === "senior_analyst" ? "escalated" : "pending_review";
}

// What each role may do: the Roles table in docs/auth.md. The backend enforces it (the agent
// service's MAY_ACT and REFUSED_DECISIONS in services/agent/app/api/reviews.py, and the
// gateway's routes); the app follows it, so each role only sees what it can use.

// The roles that may start a review of a case, and decide it, in each case status. A case in
// any other status (approved, denied, docs_requested) can't take a decision.
export const MAY_DECIDE = {
  pending_review: ["analyst", "senior_analyst"],
  escalated: ["senior_analyst"],
};

// The decisions a case in that status can't take.
const REFUSED_DECISIONS = {
  escalated: ["escalate"],
};

// The roles that see the override rate (GET /metrics/override-rate).
export const METRICS_ROLES = ["senior_analyst"];

const ROLE_NAMES = { analyst: "an analyst", senior_analyst: "a senior analyst" };

// Whether this role may decide a case in this status. Only then does opening the case start
// its review.
export function canDecide(role, status) {
  return (MAY_DECIDE[status] ?? []).includes(role);
}

// The decisions this role may choose for a case in this status, as DECISIONS entries: none
// if it can't decide the case.
export function allowedDecisions(role, status) {
  if (!canDecide(role, status)) return [];
  const refused = REFUSED_DECISIONS[status] ?? [];
  return DECISIONS.filter((option) => !refused.includes(option.value));
}

// Why a case is read-only for whoever can't decide it.
export function readOnlyReason(status) {
  const roles = MAY_DECIDE[status];
  if (!roles) return `This case is ${label(status)}, so it can't take a new decision.`;
  return `This case is ${label(status)}. Only ${roles.map((role) => ROLE_NAMES[role] ?? role).join(" or ")} can decide it.`;
}

export function isOverride(recommendation, decision) {
  return !(AGREES_WITH[recommendation] ?? []).includes(decision);
}

// pending_review -> "Pending review"
export function label(value) {
  if (!value) return "";
  const text = String(value).replace(/_/g, " ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function percent(rate) {
  return rate === null || rate === undefined ? "—" : `${Math.round(rate * 100)}%`;
}

export function formatDate(value) {
  return value ? new Date(value).toLocaleString() : "";
}
