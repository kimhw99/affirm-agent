import { describe, expect, it } from "vitest";
import { CASE_STATUSES, canDecide, defaultQueue, isOverride, label, percent } from "./format.js";

describe("format", () => {
  it("starts each role on its own queue", () => {
    expect(defaultQueue("analyst")).toBe("pending_review");
    expect(defaultQueue("senior_analyst")).toBe("escalated");
  });

  it("lets only pending and escalated cases take a decision", () => {
    expect(CASE_STATUSES.filter(canDecide)).toEqual(["pending_review", "escalated"]);
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
