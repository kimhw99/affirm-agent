import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import caseInput from "../../contracts/examples/case_input/case_1042.json";
import reviewOutput from "../../services/agent/examples/review_output_case_1042.json";
import { AppRoutes, ROUTER_FUTURE } from "./App.jsx";
import { AuthProvider, SESSION_KEY } from "./auth.jsx";

const USER = { user_id: "u_analyst_01", username: "analyst", display_name: "Demo Analyst", role: "analyst" };
const SUMMARY = {
  case_id: "case_1042",
  status: "pending_review",
  legal_name: "NORTHLINE BICYCLE WORKS LLC",
  dba: "Northline Cycles",
  state: "IL",
  flag_reasons: ["NAME_SIMILAR_MATCH", "TIN_MISMATCH"],
  created_at: "2026-11-19T10:00:00Z",
  updated_at: "2026-11-19T10:00:00Z",
};
const DECISION = {
  review_id: "rv_1",
  case_id: "case_1042",
  decision: "approve",
  note: "Name change confirmed.",
  analyst_id: "u_analyst_01",
  agent_recommendation: "recommend_clear",
  overridden: false,
  decided_at: "2026-11-20T10:00:00Z",
};

// Answers fetch from a table of "METHOD /api/path" -> body, or a function that returns the body.
function mockApi(routes) {
  const calls = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options = {}) => {
    const key = `${options.method ?? "GET"} ${url}`;
    calls.push({ key, headers: options.headers, body: options.body ? JSON.parse(options.body) : undefined });
    const answer = routes[key];
    if (answer === undefined) return new Response(JSON.stringify({ detail: `no mock for ${key}` }), { status: 404 });
    const body = typeof answer === "function" ? answer() : answer;
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  return calls;
}

function renderAt(path) {
  return render(
    <AuthProvider>
      <MemoryRouter initialEntries={[path]} future={ROUTER_FUTURE}>
        <AppRoutes />
      </MemoryRouter>
    </AuthProvider>,
  );
}

function logIn() {
  localStorage.setItem(SESSION_KEY, JSON.stringify({ token: "test-token", user: USER, expiresAt: Date.now() + 3_600_000 }));
}

describe("review app", () => {
  it("logs in and shows the analyst's queue with the override rate", async () => {
    const calls = mockApi({
      "POST /api/auth/login": { access_token: "test-token", token_type: "bearer", expires_in: 3600, user: USER },
      "GET /api/cases?status=pending_review": [SUMMARY],
      "GET /api/metrics/override-rate": { decisions: 4, overridden: 1, rate: 0.25, by_recommendation: [] },
    });
    renderAt("/login");
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "analyst" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "a password" } });
    fireEvent.click(screen.getByRole("button", { name: "Log in" }));

    expect(await screen.findByRole("link", { name: "case_1042" })).toBeTruthy();
    expect(await screen.findByText("25%")).toBeTruthy();
    expect(calls[0].body).toEqual({ username: "analyst", password: "a password" });
    const queue = calls.find((call) => call.key === "GET /api/cases?status=pending_review");
    expect(queue.headers.Authorization).toBe("Bearer test-token");
  });

  it("shows the memo, explains an evidence ID and saves the decision", async () => {
    logIn();
    let status = "pending_review";
    const calls = mockApi({
      "GET /api/cases/case_1042": () => ({ ...caseInput, status }),
      "POST /api/reviews": { review_id: "rv_1", created: true, status: "awaiting_decision", review_output: reviewOutput },
      "POST /api/reviews/rv_1/decision": () => {
        status = "approved";
        return { review_id: "rv_1", status: "done", review_output: reviewOutput, decision_record: DECISION };
      },
    });
    renderAt("/cases/case_1042");

    expect((await screen.findAllByText("Recommend clear")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getAllByRole("button", { name: "B1" })[0]);
    expect(screen.getByLabelText("Evidence B1").textContent).toContain(
      "Account holder: NORTHLINE BICYCLE WORKS LLC DBA NORTHLINE CYCLES",
    );

    fireEvent.click(screen.getByLabelText("Approve"));
    fireEvent.change(screen.getByLabelText("Note"), { target: { value: "Name change confirmed." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit decision" }));

    expect(await screen.findByText("Decision saved")).toBeTruthy();
    expect(await screen.findByText("The case is now Approved.")).toBeTruthy();
    const decision = calls.find((call) => call.key === "POST /api/reviews/rv_1/decision");
    expect(decision.body).toEqual({ decision: "approve", note: "Name change confirmed." });
  });

  it("warns when the decision goes against the recommendation", async () => {
    logIn();
    mockApi({
      "GET /api/cases/case_1042": { ...caseInput, status: "pending_review" },
      "POST /api/reviews": { review_id: "rv_1", created: false, status: "awaiting_decision", review_output: reviewOutput },
    });
    renderAt("/cases/case_1042");
    fireEvent.click(await screen.findByLabelText("Deny"));
    expect(screen.getByText(/counts as an override/)).toBeTruthy();
  });

  it("waits for a running review", async () => {
    logIn();
    mockApi({
      "GET /api/cases/case_1042": { ...caseInput, status: "pending_review" },
      "POST /api/reviews": { review_id: "rv_1", created: true, status: "running" },
      "GET /api/reviews/rv_1": { review_id: "rv_1", status: "awaiting_decision", review_output: reviewOutput },
    });
    renderAt("/cases/case_1042");
    expect(await screen.findByText(/The agent is reviewing this case/)).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Submit decision" }, { timeout: 4000 })).toBeTruthy();
  });

  it("shows a decided case's decisions without starting a review", async () => {
    logIn();
    const calls = mockApi({
      "GET /api/cases/case_1042": { ...caseInput, status: "approved" },
      "GET /api/cases/case_1042/decisions": [DECISION],
    });
    renderAt("/cases/case_1042");
    expect(await screen.findByText(/can't take a new decision/)).toBeTruthy();
    expect(screen.getByText(/Name change confirmed/)).toBeTruthy();
    expect(calls.some((call) => call.key === "POST /api/reviews")).toBe(false);
  });

  it("goes back to the login page when the session has expired", async () => {
    logIn();
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async () => new Response(JSON.stringify({ detail: "the session has expired; log in again" }), { status: 401 }),
    );
    renderAt("/");
    expect(await screen.findByRole("button", { name: "Log in" })).toBeTruthy();
    expect(localStorage.getItem(SESSION_KEY)).toBeNull();
  });
});
