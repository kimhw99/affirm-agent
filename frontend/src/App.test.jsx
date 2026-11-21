import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import caseInput from "../../contracts/examples/case_input/case_1042.json";
import reviewOutput from "../../services/agent/examples/review_output_case_1042.json";
import { AppRoutes, ROUTER_FUTURE } from "./App.jsx";
import { AuthProvider, SESSION_KEY } from "./auth.jsx";

const USER = { user_id: "u_analyst_01", username: "analyst", display_name: "Demo Analyst", role: "analyst" };
const SENIOR = { user_id: "u_senior_01", username: "senior", display_name: "Demo Senior Analyst", role: "senior_analyst" };
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
const ESCALATION = {
  ...DECISION,
  decision: "escalate",
  note: "Needs a second look.",
  agent_recommendation: "insufficient_evidence",
};
const DOCS_REQUEST = { ...DECISION, decision: "request_docs", note: "Ask for the IRS 147C letter." };

// An answer with a status other than 200, for mockApi.
class Reply {
  constructor(status, body) {
    this.status = status;
    this.body = body;
  }
}

// Answers fetch from a table of "METHOD /api/path" -> body (or a Reply), or a function that returns one.
function mockApi(routes) {
  const calls = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options = {}) => {
    const key = `${options.method ?? "GET"} ${url}`;
    calls.push({ key, headers: options.headers, body: options.body ? JSON.parse(options.body) : undefined });
    const answer = routes[key];
    if (answer === undefined) return new Response(JSON.stringify({ detail: `no mock for ${key}` }), { status: 404 });
    const answered = typeof answer === "function" ? answer() : answer;
    const { status, body } = answered instanceof Reply ? answered : new Reply(200, answered);
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
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

function logIn(user = USER) {
  localStorage.setItem(SESSION_KEY, JSON.stringify({ token: "test-token", user, expiresAt: Date.now() + 3_600_000 }));
}

describe("review app", () => {
  it("logs in and shows the analyst's queue, without the metrics", async () => {
    const calls = mockApi({
      "POST /api/auth/login": { access_token: "test-token", token_type: "bearer", expires_in: 3600, user: USER },
      "GET /api/cases?status=pending_review": [SUMMARY],
    });
    renderAt("/login");
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "analyst" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "a password" } });
    fireEvent.click(screen.getByRole("button", { name: "Log in" }));

    expect(await screen.findByRole("link", { name: "case_1042" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Metrics" })).toBeNull();
    expect(calls.some((call) => call.key.includes("/metrics"))).toBe(false);
    expect(calls[0].body).toEqual({ username: "analyst", password: "a password" });
    const queue = calls.find((call) => call.key === "GET /api/cases?status=pending_review");
    expect(queue.headers.Authorization).toBe("Bearer test-token");
  });

  it("logs a refused login and a login to the console, without the password or the token", async () => {
    const consoleLines = ["info", "warn"].map((method) => vi.spyOn(console, method).mockImplementation(() => {}));
    let attempts = 0;
    mockApi({
      "POST /api/auth/login": () =>
        attempts++ === 0
          ? new Reply(401, { detail: "wrong username or password" })
          : { access_token: "test-token", token_type: "bearer", expires_in: 3600, user: USER },
      "GET /api/cases?status=pending_review": [SUMMARY],
    });
    renderAt("/login");
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "analyst" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "a password" } });
    fireEvent.click(screen.getByRole("button", { name: "Log in" }));
    expect(await screen.findByText("Wrong username or password.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Log in" }));
    expect(await screen.findByRole("link", { name: "case_1042" })).toBeTruthy();

    const written = consoleLines.flatMap((spy) => spy.mock.calls.map(([line]) => line));
    expect(written.some((line) => / WARNING login\.failed status=401$/.test(line))).toBe(true);
    expect(written.some((line) => / INFO login user_id=u_analyst_01 role=analyst$/.test(line))).toBe(true);
    expect(written.join("\n")).not.toMatch(/a password|test-token/);
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
    expect(screen.getByLabelText("Escalate")).toBeTruthy();       // a pending case offers every decision
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

  it("shows an analyst an escalated case read-only, without starting a review", async () => {
    logIn();
    const calls = mockApi({
      "GET /api/cases/case_1042": { ...caseInput, status: "escalated" },
      "GET /api/cases/case_1042/decisions": [ESCALATION],
    });
    renderAt("/cases/case_1042");
    expect(await screen.findByText("This case is Escalated. Only a senior analyst can decide it.")).toBeTruthy();
    expect(await screen.findByText(/Needs a second look/)).toBeTruthy();
    expect(calls.some((call) => call.key === "POST /api/reviews")).toBe(false);
    expect(screen.queryByRole("alert")).toBeNull();                 // no error, just the explanation
  });

  it("offers a senior analyst every decision but Escalate on an escalated case", async () => {
    logIn(SENIOR);
    const calls = mockApi({
      "GET /api/cases/case_1042": { ...caseInput, status: "escalated" },
      "POST /api/reviews": { review_id: "rv_2", created: true, status: "awaiting_decision", review_output: reviewOutput },
    });
    renderAt("/cases/case_1042");
    expect(await screen.findByLabelText("Approve")).toBeTruthy();
    expect(screen.getByLabelText("Request documents")).toBeTruthy();
    expect(screen.getByLabelText("Deny")).toBeTruthy();
    expect(screen.queryByLabelText("Escalate")).toBeNull();
    expect(calls.some((call) => call.key === "POST /api/reviews")).toBe(true);
  });

  it("links a senior analyst to the metrics, which show the override rate", async () => {
    logIn(SENIOR);
    mockApi({
      "GET /api/cases?status=escalated": [{ ...SUMMARY, status: "escalated" }],
      "GET /api/metrics/override-rate": { decisions: 4, overridden: 1, rate: 0.25, by_recommendation: [] },
    });
    renderAt("/");
    fireEvent.click(await screen.findByRole("link", { name: "Metrics" }));
    expect(await screen.findByText("25%")).toBeTruthy();
    expect(screen.getByText(/1 of 4 decisions went against/)).toBeTruthy();
  });

  it("keeps an analyst out of the metrics page without asking the backend", async () => {
    logIn();
    const calls = mockApi({});
    renderAt("/metrics");
    expect(await screen.findByText("You don't have access to this page.")).toBeTruthy();
    expect(screen.getByRole("link", { name: "← Case queue" })).toBeTruthy();
    expect(calls).toEqual([]);
  });

  it("says when the backend refuses an action, and stays logged in", async () => {
    logIn();
    mockApi({
      // The case was escalated after it was loaded, so the agent refuses the analyst's review.
      "GET /api/cases/case_1042": { ...caseInput, status: "pending_review" },
      "POST /api/reviews": new Reply(403, { detail: "only senior_analyst can review a case that is escalated" }),
    });
    renderAt("/cases/case_1042");
    expect(await screen.findByText(/You don't have permission to do this\./)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Log out" })).toBeTruthy();
    expect(localStorage.getItem(SESSION_KEY)).not.toBeNull();
  });

  it("adds the merchant's document to a docs_requested case, then starts its new review", async () => {
    logIn();
    let status = "docs_requested";
    const calls = mockApi({
      "GET /api/cases/case_1042": () => ({ ...caseInput, status }),
      "GET /api/cases/case_1042/decisions": [DOCS_REQUEST],
      "POST /api/cases/case_1042/documents": () => {
        status = "pending_review";
        return { document: { id: "D2", type: "irs_147c", extracted: { name: "NORTHLINE BICYCLE WORKS LLC", ein_last4: "4567" },
                             confidence: 1 }, case_status: "pending_review" };
      },
      "POST /api/reviews": { review_id: "rv_2", created: true, status: "awaiting_decision", review_output: reviewOutput },
    });
    renderAt("/cases/case_1042");

    expect(await screen.findByRole("heading", { name: "Add the documents the merchant sent" })).toBeTruthy();
    expect(await screen.findByText(/Ask for the IRS 147C letter/)).toBeTruthy();   // the decision that asked for it
    expect(screen.getByText(/only its last 4 digits, never a full SSN or EIN/)).toBeTruthy();
    expect(calls.some((call) => call.key === "POST /api/reviews")).toBe(false);

    fireEvent.change(screen.getByLabelText("Document type"), { target: { value: "irs_147c" } });
    for (const field of ["Name", "EIN, last 4 digits", "IRS name control", "Address", "Date"]) {
      expect(screen.getByLabelText(field)).toBeTruthy();
    }
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: " NORTHLINE BICYCLE WORKS LLC " } });
    fireEvent.change(screen.getByLabelText("EIN, last 4 digits"), { target: { value: "4567" } });
    fireEvent.change(screen.getByLabelText("Note"), { target: { value: "Sent by email." } });
    fireEvent.click(screen.getByRole("button", { name: "Add the document" }));
    expect(screen.getByRole("button", { name: "Adding…" })).toBeTruthy();

    expect(await screen.findByText(/Added D2: IRS letter 147C/)).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Submit decision" })).toBeTruthy();   // the new review's form
    expect(screen.queryByRole("heading", { name: "Add the documents the merchant sent" })).toBeNull();
    const keys = calls.map((call) => call.key);
    expect(keys.indexOf("POST /api/cases/case_1042/documents")).toBeLessThan(keys.indexOf("POST /api/reviews"));
    expect(calls.find((call) => call.key === "POST /api/cases/case_1042/documents").body).toEqual({
      type: "irs_147c", extracted: { name: "NORTHLINE BICYCLE WORKS LLC", ein_last4: "4567" }, note: "Sent by email." });
  });

  it("shows why the backend refused a document, and keeps the form", async () => {
    logIn();
    let answer = new Reply(422, { detail: "a full SSN or EIN appears in the document or the note; enter only the last 4 digits" });
    mockApi({
      "GET /api/cases/case_1042": { ...caseInput, status: "docs_requested" },
      "GET /api/cases/case_1042/decisions": [DOCS_REQUEST],
      "POST /api/cases/case_1042/documents": () => answer,
    });
    renderAt("/cases/case_1042");
    fireEvent.change(await screen.findByLabelText("Document type"), { target: { value: "w9" } });
    fireEvent.change(screen.getByLabelText("Legal name"), { target: { value: "Northline Bicycle Works LLC" } });
    fireEvent.click(screen.getByRole("button", { name: "Add the document" }));
    expect(await screen.findByText(/Couldn't add the document: a full SSN or EIN appears/)).toBeTruthy();

    // A validation error from FastAPI comes as a list; its messages are shown, with where they are.
    answer = new Reply(422, { detail: [{ loc: ["body", "extracted", "legal_name"], msg: "Input should be a valid string" }] });
    fireEvent.click(screen.getByRole("button", { name: "Add the document" }));
    expect(await screen.findByText(/extracted\.legal_name: Input should be a valid string/)).toBeTruthy();
    expect(screen.getByLabelText("Legal name").value).toBe("Northline Bicycle Works LLC");
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
