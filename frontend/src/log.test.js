import { describe, expect, it, vi } from "vitest";
import { ApiError, request } from "./api.js";
import { line, log } from "./log.js";

const TIME = "\\d{4}-\\d\\d-\\d\\dT\\d\\d:\\d\\d:\\d\\d\\.\\d{3}Z";
const JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1X2FuYWx5c3RfMDEifQ.c2lnbmF0dXJl";

describe("log", () => {
  it("writes one line with the UTC time, the level, the event and its details", () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => {});
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    log.info("review.started", { case_id: "case_1042", review_id: "rv_1", created: true });
    log.warn("login.failed", { status: 401 });
    log.error("api.failed", { method: "GET", path: "/cases", reason: "the case service isn't reachable" });
    expect(info.mock.calls[0][0]).toMatch(
      new RegExp(`^${TIME} INFO review\\.started case_id=case_1042 review_id=rv_1 created=true$`),
    );
    expect(warn.mock.calls[0][0]).toMatch(new RegExp(`^${TIME} WARNING login\\.failed status=401$`));
    expect(error.mock.calls[0][0]).toMatch(/ ERROR api\.failed method=GET path=\/cases reason="the case service isn't reachable"$/);
  });

  it("leaves out details without a value", () => {
    expect(line("INFO", "login", { user_id: "u_1", request_id: null, status: undefined })).toMatch(/ INFO login user_id=u_1$/);
    expect(line("INFO", "login")).toMatch(/ INFO login$/);
  });

  it("never writes a password, a token or an API key", () => {
    const written = line("ERROR", "oops", {
      password: "a password",
      access_token: JWT,
      Authorization: `Bearer ${JWT}`,
      reason: `failed with ${JWT}, Bearer abc.def and sk-proj-1234567890abcdef`,
    });
    for (const secret of ["a password", "eyJ", "abc.def", "sk-proj"]) expect(written).not.toContain(secret);
    expect(written).toContain("password=[redacted] access_token=[redacted] Authorization=[redacted]");
  });
});

describe("request", () => {
  it("logs a refused call with its request ID, but not its token or body", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "only senior_analyst can see /metrics" }), {
        status: 403,
        headers: { "Content-Type": "application/json", "X-Request-ID": "req-403" },
      }),
    );
    const refused = await request("/metrics/override-rate?days=30", { token: JWT, method: "POST", body: { note: "private" } })
      .catch((error) => error);
    expect(refused).toBeInstanceOf(ApiError);
    expect(refused.requestId).toBe("req-403");
    const [[written]] = warn.mock.calls;
    expect(written).toMatch(
      / WARNING api\.failed method=POST path=\/metrics\/override-rate status=403 request_id=req-403 reason="only senior_analyst can see \/metrics"$/,
    );
    expect(written).not.toContain("eyJ");
    expect(written).not.toContain("private");
  });

  it("logs a 5xx answer and a backend out of reach as errors", async () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response("<html>Bad Gateway</html>", { status: 502 }))
      .mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await expect(request("/cases")).rejects.toMatchObject({ status: 502, message: "HTTP 502" });
    await expect(request("/cases")).rejects.toThrow("Failed to fetch");
    expect(error.mock.calls.map(([written]) => written.replace(/^\S+ /, ""))).toEqual([
      'ERROR api.failed method=GET path=/cases status=502 reason="HTTP 502"',
      'ERROR api.unreachable method=GET path=/cases reason="Failed to fetch"',
    ]);
  });

  it("logs nothing for a call that works", async () => {
    const logged = ["info", "warn", "error"].map((method) => vi.spyOn(console, method));
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify([]), { status: 200 }));
    expect(await request("/cases")).toEqual([]);
    for (const spy of logged) expect(spy).not.toHaveBeenCalled();
  });
});
