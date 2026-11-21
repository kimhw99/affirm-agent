import { describe, expect, it } from "vitest";
import caseInputSource from "../../contracts/kyb_contracts/case_input.py?raw";
import {
  CASE_STATUSES,
  DOCUMENT_TYPES,
  METRICS_ROLES,
  allowedDecisions,
  canDecide,
  defaultQueue,
  documentLabel,
  fieldLabel,
  isOverride,
  label,
  percent,
  readOnlyReason,
} from "./format.js";

const ROLES = ["analyst", "senior_analyst"];
const values = (options) => options.map((option) => option.value);

describe("format", () => {
  it("starts each role on its own queue", () => {
    expect(defaultQueue("analyst")).toBe("pending_review");
    expect(defaultQueue("senior_analyst")).toBe("escalated");
  });

  it("follows the override rule in decision.py", () => {
    expect(isOverride("recommend_clear", "approve")).toBe(false);
    expect(isOverride("recommend_clear", "deny")).toBe(true);
    expect(isOverride("insufficient_evidence", "request_docs")).toBe(false);
    expect(isOverride("escalate", "deny")).toBe(false);
  });

  it("turns codes into labels and rates into percentages", () => {
    expect(label("pending_review")).toBe("Pending review");
    expect(percent(0.3333)).toBe("33%");
    expect(percent(null)).toBe("—");
  });
});

// The document types and their fields, as contracts/kyb_contracts/case_input.py has them.
function contractDocuments(source) {
  const types = [...source.match(/DocumentType = Literal\[([\s\S]*?)\]/)[1].matchAll(/"(\w+)"/g)].map((m) => m[1]);
  const block = source.match(/DOCUMENT_FIELDS: dict\[str, set\[str\]\] = \{([\s\S]*?)\n\}/)[1];
  const fields = Object.fromEntries([...block.matchAll(/"(\w+)": \{([^}]*)\}/g)].map(([, type, list]) => [
    type, [...list.matchAll(/"(\w+)"/g)].map((m) => m[1]).sort()]));
  return { types, fields };
}

describe("documents (case_input.py)", () => {
  it("offers every DocumentType, in the contract's order, with exactly its DOCUMENT_FIELDS", () => {
    const contract = contractDocuments(caseInputSource);
    expect(contract.types).toHaveLength(8);
    expect(values(DOCUMENT_TYPES)).toEqual(contract.types);
    expect(Object.fromEntries(DOCUMENT_TYPES.map((t) => [t.value, [...t.fields].sort()]))).toEqual(contract.fields);
    for (const type of DOCUMENT_TYPES) expect(new Set(type.fields).size).toBe(type.fields.length);
  });

  it("names the documents and their fields for people", () => {
    expect(documentLabel("irs_147c")).toBe("IRS letter 147C (the EIN and the name on file)");
    expect(fieldLabel("ein_last4")).toBe("EIN, last 4 digits");
    expect(fieldLabel("legal_name")).toBe("Legal name");
    expect(DOCUMENT_TYPES.every((t) => t.label && !t.label.includes("_"))).toBe(true);
  });
});

// One test per row of the Roles table in docs/auth.md. Seeing the queue, a case and its
// decisions is open to both roles, so no rule here limits it.
describe("roles (docs/auth.md)", () => {
  it("shows the override rate to senior analysts only", () => {
    expect(METRICS_ROLES).toEqual(["senior_analyst"]);
  });

  it("lets either role decide a pending_review case, with every decision", () => {
    for (const role of ROLES) {
      expect(canDecide(role, "pending_review")).toBe(true);
      expect(values(allowedDecisions(role, "pending_review"))).toEqual(["approve", "request_docs", "escalate", "deny"]);
    }
  });

  it("lets only a senior analyst decide an escalated case, and not escalate it again", () => {
    expect(canDecide("analyst", "escalated")).toBe(false);
    expect(allowedDecisions("analyst", "escalated")).toEqual([]);
    expect(canDecide("senior_analyst", "escalated")).toBe(true);
    expect(values(allowedDecisions("senior_analyst", "escalated"))).toEqual(["approve", "request_docs", "deny"]);
    expect(readOnlyReason("escalated")).toBe("This case is Escalated. Only a senior analyst can decide it.");
  });

  it("lets nobody decide an approved, denied or docs_requested case", () => {
    for (const status of ["approved", "denied", "docs_requested"]) {
      for (const role of ROLES) {
        expect(canDecide(role, status)).toBe(false);
        expect(allowedDecisions(role, status)).toEqual([]);
      }
      expect(readOnlyReason(status)).toBe(`This case is ${label(status)}, so it can't take a new decision.`);
    }
  });

  it("covers every case status, and lets no other role decide anything", () => {
    expect(CASE_STATUSES.filter((status) => canDecide("senior_analyst", status))).toEqual(["pending_review", "escalated"]);
    expect(CASE_STATUSES.filter((status) => canDecide("analyst", status))).toEqual(["pending_review"]);
    expect(CASE_STATUSES.some((status) => canDecide("admin", status))).toBe(false);
  });
});
