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

// Only these cases can take a decision, so only they get a review when opened.
export function canDecide(status) {
  return status === "pending_review" || status === "escalated";
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
