"""Checks that the running system refuses requests beyond a user's role, or from no user.

The golden dataset's unsafe cases include "an analyst going beyond their role"
(docs/proposal/proposal.md, section 9). That depends on who sends a request, not on a
case's data, so it has no case folder in data/golden: this script sends those requests
to the running system and checks each answer against the Roles table in docs/auth.md.

It logs in as the demo analyst and the demo senior analyst through the gateway, and
starts the analyst's review of the case, which runs while the first checks do. Then:
1. While the case is pending_review: tokens that must not work (none, one signed with
   another key, an expired one, an unsigned one), the override rate as an analyst and
   with a forged X-User-Role, a decision written from the browser, a review of a case
   sent with the request, documents for a case that isn't waiting for them, and the
   case service on this machine without the service token.
2. The analyst escalates the case. A review of it as an analyst, with or without a
   forged X-User-Role.
3. The senior analyst starts a review of it. The analyst deciding that review, and the
   senior analyst escalating the case again.
4. The senior analyst approves the case. A review of it, as either role.

So it changes the case (case_1042 by default), as steps 5 and 12 of
docs/local-testing.md do: run it on freshly loaded data, with the app running.

    docker compose run --rm case python seed.py --reset
    docker compose restart agent        # the agent keeps its reviews in memory
    python scripts/check_access.py --out /tmp/access-results.md

Usage, from the repo root (needs httpx and pyjwt, which the gateway's requirements install):

    python scripts/check_access.py [--env .env] [--gateway URL] [--case-service URL]
                                   [--case CASE_ID] [--timeout SECONDS] [--out FILE]

From .env it reads the demo users' passwords, CASE_SERVICE_TOKEN, JWT_SECRET (to sign
the expired token, so that its date is its only flaw) and the ports. It never prints
or writes any of them, nor a token: everything it prints or writes goes through
redact() first, as a last guard.

It prints PASS or FAIL for each check, with the status code it got, and "N of M
passed" last; --out writes the same as a Markdown table. It exits with 0 if every
check passed, 1 if one failed or couldn't run, and 2 if it couldn't start: no .env,
the app isn't up, a login failed, the case isn't freshly loaded, or the agent still has
its review from before the data was loaded again.
"""

import argparse
import re
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jwt

# The .env values the script needs. Each one is a secret: never printed or written.
PASSWORDS = {"analyst": "DEMO_ANALYST_PASSWORD", "senior": "DEMO_SENIOR_PASSWORD"}
SECRET_VARIABLES = (*PASSWORDS.values(), "CASE_SERVICE_TOKEN", "JWT_SECRET")
RELOAD = "docker compose run --rm case python seed.py --reset, then docker compose restart agent"
POLL_SECONDS = 1.0
FORGED_USER = {"X-User-Id": "u_senior_01", "X-User-Role": "senior_analyst"}
UNDERSCORED_USER = {"X_User_Id": "u_senior_01", "X_User_Role": "senior_analyst"}


class CantStart(Exception):
    """The checks can't run at all: nothing has been changed yet."""


class SetupFailed(Exception):
    """A step that brings the case to its next status failed, so the checks after it can't run."""


# ---------- .env ----------

def read_env(path: Path) -> dict[str, str]:
    """The variables of a .env file, read as docker compose reads the usual lines: KEY=value,
    "export " allowed, the value optionally in quotes, and # comments. An unquoted value
    ends where " #" starts a comment."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, equals, value = line.partition("=")
        if not equals or line.startswith("#"):
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0]
        values[key.strip()] = value
    return values


# ---------- Tokens ----------

def claims_of(token: str) -> dict:
    """A JWT's claims, read without checking it, to forge tokens for the same user."""
    return jwt.decode(token, options={"verify_signature": False})


def expired_token(claims: dict, secret: str, now: int) -> str:
    """claims signed with the real secret, issued two hours ago and expired an hour ago."""
    return jwt.encode(claims | {"iat": now - 7200, "exp": now - 3600}, secret, algorithm="HS256")


def other_key_token(claims: dict, now: int) -> str:
    """claims, good for another hour, signed with a random key instead of JWT_SECRET."""
    return jwt.encode(claims | {"iat": now, "exp": now + 3600}, secrets.token_urlsafe(32), algorithm="HS256")


def unsigned_token(claims: dict, now: int) -> str:
    """claims, good for another hour, with alg none and no signature."""
    return jwt.encode(claims | {"iat": now, "exp": now + 3600}, None, algorithm="none")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------- Checks and their results ----------

@dataclass
class Check:
    """A request someone must not get away with, or a control that must work."""

    name: str
    who: str                  # nobody, analyst, senior, or service (with the service token)
    request: str              # what is sent, as the results describe it: never a token
    expected: int
    why: str                  # the rule, and where docs/auth.md has it
    send: Callable[[], httpx.Response]
    reason_has: str = ""      # words the answer's reason must have, where the status alone is ambiguous


@dataclass
class Result:
    check: Check
    status: int | None        # None: no answer, or not run
    reason: str               # what the service said, or why there's no answer
    passed: bool
    ran: bool = True


def reason(response: httpx.Response) -> str:
    """What the service said: FastAPI's detail, the first of its validation errors, or the status line."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail and isinstance(detail[0], dict):
        where = ".".join(str(part) for part in detail[0].get("loc", ()))
        first = f"{where}: {detail[0].get('msg', '')}" if where else str(detail[0].get("msg", ""))
        return first + (f" (and {len(detail) - 1} more)" if len(detail) > 1 else "")
    return response.reason_phrase


def run_check(check: Check) -> Result:
    try:
        response = check.send()
    except httpx.HTTPError as error:
        return Result(check, None, f"no answer ({type(error).__name__})", False)
    said = reason(response)
    passed = response.status_code == check.expected and check.reason_has.lower() in said.lower()
    return Result(check, response.status_code, said, passed)


def not_run(check: Check, why: str) -> Result:
    return Result(check, None, why, False, ran=False)


# ---------- The run ----------

class Run:
    """The checks against one running system, in order, with the steps that change the case between them."""

    def __init__(self, gateway: httpx.Client, case_service: httpx.Client, env: dict[str, str], case_id: str,
                 timeout: float = 300, say: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.gateway, self.case_service, self.env, self.case_id = gateway, case_service, env, case_id
        self.timeout, self.say, self.clock, self.sleep = timeout, say, clock, sleep
        self.hidden = [env.get(name, "") for name in SECRET_VARIABLES]   # what redact() blanks out
        self.tokens: dict[str, str] = {}
        self.forged: dict[str, str] = {}
        self.case: dict = {}
        self.first_review: dict = {}     # the analyst's review, started before the checks
        self.first_started = 0.0
        self.senior_review = ""
        self.setup: list[str] = []       # what was done to the case, for the results

    # --- before anything changes ---

    def start(self) -> None:
        """Logs in both users, checks that the case is freshly loaded, and starts the analyst's review
        of it, which the checks don't wait for; CantStart if any of that fails."""
        missing = [name for name in SECRET_VARIABLES if not self.env.get(name)]
        if missing:
            raise CantStart(f"{', '.join(missing)} empty or missing in the env file")
        try:
            health = self.gateway.get("/health")
        except httpx.HTTPError as error:
            raise CantStart(f"the gateway at {self.gateway.base_url} doesn't answer ({type(error).__name__}): "
                            "start the app with docker compose up -d --build --wait") from None
        if health.status_code != 200:
            raise CantStart(f"the gateway's /health answered {health.status_code}")
        for user, variable in PASSWORDS.items():
            response = self.gateway.post("/auth/login", json={"username": user, "password": self.env[variable]})
            if response.status_code != 200:
                raise CantStart(f"logging in as {user} got {response.status_code} ({reason(response)}): check "
                                f"{variable} in .env; after changing it, docker compose up -d")
            self.tokens[user] = response.json()["access_token"]
            self.hidden.append(self.tokens[user])
        now, claims = int(time.time()), claims_of(self.tokens["analyst"])
        self.forged = {"expired": expired_token(claims, self.env["JWT_SECRET"], now),
                       "other_key": other_key_token(claims, now), "unsigned": unsigned_token(claims, now)}
        self.hidden += self.forged.values()
        try:
            self.case = self.read_case()
        except SetupFailed as error:
            raise CantStart(str(error)) from None
        if self.case.get("status") != "pending_review":
            raise CantStart(f"{self.case_id} is {self.case.get('status')}, not pending_review, so its data isn't "
                            f"freshly loaded. Load it again: {RELOAD}")
        self.first_started = self.clock()
        try:
            self.first_review = self.start_review("analyst")
        except SetupFailed as error:
            raise CantStart(str(error)) from None
        if self.first_review.get("status") not in ("running", "awaiting_decision"):
            raise CantStart(f"the agent answered with review {self.first_review.get('review_id')}, which is "
                            f"{self.first_review.get('status')}: it still has a review from before the data was "
                            "loaded again. docker compose restart agent, then run this again")

    def read_case(self) -> dict:
        response = self.gateway.get(f"/cases/{self.case_id}", headers=self.as_user("analyst"))
        if response.status_code == 404:
            raise SetupFailed(f"there's no {self.case_id}: load the golden cases with "
                              "docker compose run --rm case python seed.py")
        if response.status_code != 200:
            raise SetupFailed(f"reading {self.case_id} got {response.status_code} ({reason(response)})")
        return response.json()

    def as_user(self, user: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        return bearer(self.tokens[user]) | (extra or {})

    def final_status(self) -> str | None:
        """The case's status after the run, or None if it can't be read."""
        try:
            return self.read_case().get("status")
        except (SetupFailed, httpx.HTTPError):
            return None

    # --- the steps that change the case ---

    def start_review(self, user: str) -> dict:
        """Starts a review of the case as user, or gets the one the agent already has for it."""
        response = self.gateway.post("/reviews", json={"case_id": self.case_id}, headers=self.as_user(user))
        if response.status_code != 202:
            raise SetupFailed(f"{user} couldn't start a review: {response.status_code} ({reason(response)})")
        return response.json()

    def review_ready(self, user: str, review: dict | None = None, started: float | None = None) -> str:
        """Waits until the review (one it starts, if none is given) awaits the decision; its review_id."""
        started = self.clock() if started is None else started
        review = review or self.start_review(user)
        while review.get("status") == "running":
            if self.clock() - started >= self.timeout:
                raise SetupFailed(f"review {review.get('review_id')} was still running after {self.timeout:.0f} s; "
                                  "try a longer --timeout")
            self.sleep(POLL_SECONDS)
            polled = self.gateway.get(f"/reviews/{review['review_id']}", headers=self.as_user(user))
            if polled.status_code != 200:
                raise SetupFailed(f"reading review {review['review_id']} got {polled.status_code} ({reason(polled)})")
            review = polled.json()
        if review.get("status") != "awaiting_decision":
            hint = " The agent still has an earlier review of the case: docker compose restart agent." \
                if review.get("status") == "done" else ""
            error = f" ({review['error']})" if review.get("error") else ""
            raise SetupFailed(f"review {review.get('review_id')} is {review.get('status')}{error}, "
                              f"not awaiting a decision.{hint}")
        self.say(f"        {user} started review {review['review_id']}: awaiting a decision after "
                 f"{self.clock() - started:.0f} s")
        return review["review_id"]

    def decide(self, user: str, review_id: str, decision: str, status_after: str) -> None:
        response = self.gateway.post(f"/reviews/{review_id}/decision", headers=self.as_user(user),
                                     json={"decision": decision, "note": "Set up by scripts/check_access.py."})
        if response.status_code != 200:
            raise SetupFailed(f"{user}'s {decision} on review {review_id} got {response.status_code} "
                              f"({reason(response)})")
        status = self.read_case().get("status")
        if status != status_after:
            raise SetupFailed(f"after {user}'s {decision}, {self.case_id} is {status}, not {status_after}")
        self.setup.append(f"{user} decided {decision} on review {review_id}: {self.case_id} is now {status}")
        self.say(f"        {self.setup[-1]}")

    def escalate(self) -> None:
        self.decide("analyst", self.review_ready("analyst", self.first_review, self.first_started), "escalate",
                    "escalated")

    def start_senior_review(self) -> None:
        self.senior_review = self.review_ready("senior")
        self.setup.append(f"senior started review {self.senior_review} of the escalated case")

    def approve(self) -> None:
        self.decide("senior", self.senior_review, "approve", "approved")

    # --- the checks ---

    def stages(self) -> list[tuple[str, Callable[[], None] | None, list[Check]]]:
        """(what the case is, the step that brings it there, the checks then), in order."""
        case, direct = f"/cases/{self.case_id}", str(self.case_service.base_url).rstrip("/")
        queue = {"status": "pending_review"}
        doctored = self.case | {"flag_reasons": []}
        decision = {"decision_record": {
            "review_id": "rv_forged0", "case_id": self.case_id, "decision": "approve",
            "note": "Written without a review.", "analyst_id": "u_analyst_01",
            "agent_recommendation": "recommend_clear", "overridden": False,
            "decided_at": datetime.now(timezone.utc).isoformat()}, "case_status": "approved"}
        letter = {"type": "irs_147c", "extracted": {"name": "NORTHLINE BICYCLE WORKS LLC", "ein_last4": "4567"},
                  "note": "Sent while the case is pending review."}
        start_review = {"case_id": self.case_id}
        gateway, case_service = self.gateway, self.case_service

        pending = [
            Check("No token", "nobody", "GET /cases?status=pending_review with no Authorization header", 401,
                  "The gateway lets /cases, /metrics and /reviews through only with a valid JWT "
                  "(What the gateway lets through).",
                  lambda: gateway.get("/cases", params=queue), "log in first"),
            Check("A token signed with another key", "nobody",
                  "GET /cases?status=pending_review with the analyst's claims, signed with a random key", 401,
                  "The gateway checks the HS256 signature with JWT_SECRET, so nobody can make up a token (The JWT).",
                  lambda: gateway.get("/cases", params=queue, headers=bearer(self.forged["other_key"])),
                  "invalid token"),
            Check("An expired token", "analyst",
                  "GET /cases?status=pending_review with the analyst's claims, signed with JWT_SECRET, "
                  "expired an hour ago", 401,
                  "A JWT stops working at its exp, though its signature is good: the user logs in again (The JWT).",
                  lambda: gateway.get("/cases", params=queue, headers=bearer(self.forged["expired"])), "expired"),
            Check("An unsigned token (alg none)", "nobody",
                  "GET /cases?status=pending_review with the analyst's claims, alg none and no signature", 401,
                  "The gateway accepts only HS256, so a token without a signature is refused (The JWT).",
                  lambda: gateway.get("/cases", params=queue, headers=bearer(self.forged["unsigned"])),
                  "invalid token"),
            Check("The override rate, as an analyst", "analyst", "GET /metrics/override-rate", 403,
                  "Only a senior analyst sees the override rate (Roles; the metrics route's roles).",
                  lambda: gateway.get("/metrics/override-rate", headers=self.as_user("analyst")),
                  "only for senior_analyst"),
            Check("The override rate, with a forged X-User-Role", "analyst",
                  "GET /metrics/override-rate with the analyst's JWT and X-User-Id: u_senior_01, "
                  "X-User-Role: senior_analyst", 403,
                  "The gateway takes the role from the JWT, and drops the X-User-* headers a client sends "
                  "(What the gateway lets through).",
                  lambda: gateway.get("/metrics/override-rate", headers=self.as_user("analyst", FORGED_USER)),
                  "only for senior_analyst"),
            Check("The override rate, as a senior analyst", "senior", "GET /metrics/override-rate", 200,
                  "Control: a senior analyst may see it (Roles), so the two 403s above are about the role.",
                  lambda: gateway.get("/metrics/override-rate", headers=self.as_user("senior"))),
            Check("A decision written from the browser", "analyst",
                  f"POST {case}/decisions with a DecisionRecord (approve) and case_status approved", 405,
                  "Only the agent service saves a decision, at the end of a review (step 11). The gateway lets "
                  "only the merchant's documents through to the case service as a write "
                  "(What the gateway lets through).",
                  lambda: gateway.post(f"{case}/decisions", json=decision, headers=self.as_user("analyst")),
                  "isn't allowed"),
            Check("A review of a case sent with the request", "analyst",
                  "POST /reviews with case_id and the whole case, its flags removed", 422,
                  "POST /reviews takes only case_id, and the agent reads the case from the case service, so a "
                  "request can't have a doctored copy reviewed (Roles).",
                  lambda: gateway.post("/reviews", json=start_review | {"case": doctored},
                                       headers=self.as_user("analyst"))),
            Check("Documents for a case that isn't waiting for them", "analyst",
                  f"POST {case}/documents (an IRS letter 147C) while the case is pending_review", 409,
                  "Only a docs_requested case takes the merchant's documents (Roles).",
                  lambda: gateway.post(f"{case}/documents", json=letter, headers=self.as_user("analyst")),
                  "only a docs_requested case"),
            Check("The case service without the gateway or a token", "nobody",
                  f"GET {direct}{case} with no Authorization header", 401,
                  "The case service answers only with the service token, which only the gateway and the agent "
                  "have (Service to service).",
                  lambda: case_service.get(case), "service token"),
            Check("A decision written straight to the case service, with a user's JWT", "analyst",
                  f"POST {direct}{case}/decisions with the analyst's JWT and a DecisionRecord (approve)", 401,
                  "A user's JWT isn't the service token, so going around the gateway doesn't let an analyst save "
                  "a decision (Service to service).",
                  lambda: case_service.post(f"{case}/decisions", json=decision, headers=self.as_user("analyst")),
                  "service token"),
            Check("The case service with the service token", "service",
                  f"GET {direct}{case} with CASE_SERVICE_TOKEN", 200,
                  "Control: the same read with the service token works, so the 401s above are about the token.",
                  lambda: case_service.get(case, headers=bearer(self.env["CASE_SERVICE_TOKEN"]))),
        ]
        escalated = [
            Check("A review of an escalated case, as an analyst", "analyst",
                  f"POST /reviews for {self.case_id}, now escalated", 403,
                  "Only a senior analyst reviews or decides an escalated case (Roles; MAY_ACT).",
                  lambda: gateway.post("/reviews", json=start_review, headers=self.as_user("analyst")),
                  "only senior_analyst"),
            Check("A review of an escalated case, with a forged X-User-Role", "analyst",
                  "POST /reviews with the analyst's JWT and X-User-Id: u_senior_01, X-User-Role: senior_analyst",
                  403,
                  "The gateway sends the agent the role from the JWT, never the client's X-User-* headers "
                  "(What the gateway lets through).",
                  lambda: gateway.post("/reviews", json=start_review, headers=self.as_user("analyst", FORGED_USER)),
                  "only senior_analyst"),
            Check("A review of an escalated case, with X_User_Role spelled with underscores", "analyst",
                  "POST /reviews with the analyst's JWT and X_User_Id: u_senior_01, X_User_Role: senior_analyst",
                  403,
                  "A header spelled with underscores isn't X-User-Role, which the agent reads, and which the "
                  "gateway sets from the JWT.",
                  lambda: gateway.post("/reviews", json=start_review,
                                       headers=self.as_user("analyst", UNDERSCORED_USER)),
                  "only senior_analyst"),
        ]
        senior_review = [
            Check("Deciding an escalated case, as an analyst", "analyst",
                  "POST /reviews/<the senior analyst's review>/decision with approve", 403,
                  "The agent checks the role against the case's status again before it resumes a review, so an "
                  "analyst can't decide a senior analyst's review (Roles).",
                  lambda: gateway.post(f"/reviews/{self.senior_review}/decision", json={"decision": "approve"},
                                       headers=self.as_user("analyst")),
                  "only senior_analyst"),
            Check("Escalating an escalated case again", "senior",
                  "POST /reviews/<the senior analyst's review>/decision with escalate", 409,
                  "An escalated case can't be escalated again (Roles; REFUSED_DECISIONS).",
                  lambda: gateway.post(f"/reviews/{self.senior_review}/decision", json={"decision": "escalate"},
                                       headers=self.as_user("senior")),
                  "other than escalate"),
        ]
        approved = [
            Check(f"A review of an approved case, as {article} {role}", user,
                  f"POST /reviews for {self.case_id}, now approved", 409,
                  "An approved case is closed: nobody reviews or decides it (Roles).",
                  lambda user=user: gateway.post("/reviews", json=start_review, headers=self.as_user(user)),
                  "is approved")
            for user, article, role in (("analyst", "an", "analyst"), ("senior", "a", "senior analyst"))
        ]
        return [
            (f"{self.case_id} is pending_review; nothing changes it", None, pending),
            (f"the analyst escalates {self.case_id}", self.escalate, escalated),
            ("the senior analyst starts a review of it", self.start_senior_review, senior_review),
            (f"the senior analyst approves {self.case_id}", self.approve, approved),
        ]

    def run(self) -> list[Result]:
        """Every check, in order. Once a step fails, the checks after it are listed as not run."""
        results: list[Result] = []
        stopped = ""
        for title, step, checks in self.stages():
            if not stopped:
                self.say(f"-- {title}")
                try:
                    if step:
                        step()
                except (SetupFailed, httpx.HTTPError) as error:
                    stopped = f"not run: {title} failed ({error})"
                    self.setup.append(stopped.removeprefix("not run: "))
                    self.say(f"        {self.setup[-1]}")
            for check in checks:
                result = not_run(check, stopped) if stopped else run_check(check)
                results.append(result)
                self.say(console_line(len(results), result))
        return results


# ---------- Output ----------

def redact(text: str, hidden: list[str]) -> str:
    """text with each secret value in hidden blanked out: the last guard before printing or writing."""
    for value in sorted((value for value in hidden if value and len(value) >= 4), key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return text


def label(result: Result) -> str:
    return "PASS" if result.passed else "FAIL" if result.ran else "NOT RUN"


def summary(results: list[Result]) -> str:
    return f"{sum(result.passed for result in results)} of {len(results)} passed"


def console_line(number: int, result: Result) -> str:
    got = result.status if result.status is not None else "-"
    line = f"{number:>3}  {label(result):<7}  {got} (expected {result.check.expected})  " \
           f"{result.check.who:<7}  {result.check.name}"
    return line if result.passed else f"{line}\n        {result.reason}"


def cell(text: object) -> str:
    """text fit for one Markdown table cell."""
    return " ".join(str(text).split()).replace("|", "\\|")


def markdown(results: list[Result], setup: list[str], gateway: str, case_service: str, case_id: str,
             when: datetime) -> str:
    lines = [
        "# Access checks",
        "",
        f"`scripts/check_access.py` against the gateway at {gateway} and the case service at {case_service}, "
        f"with {case_id}, on {when:%Y-%m-%d %H:%M} UTC: **{summary(results)}**.",
        "",
        "Expected: the status code, and in brackets, words the answer must have where the code alone is "
        "ambiguous. Why: the rule, with the section of docs/auth.md that has it.",
        "",
        "What was done to the case between the checks:",
        "",
        *(f"- {step}" for step in setup or ["nothing"]),
        "",
        "| # | Scenario | Who | Request | Expected | Actual | Result | The answer | Why |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for number, result in enumerate(results, 1):
        check = result.check
        expected = f"{check.expected} ({check.reason_has})" if check.reason_has else check.expected
        actual = result.status if result.status is not None else "-"
        lines.append("| " + " | ".join(cell(value) for value in (
            number, check.name, check.who, check.request, expected, actual, label(result), result.reason,
            check.why)) + " |")
    return "\n".join(lines) + "\n"


# ---------- Main ----------

def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    """The command line. transport is for the tests: a fake system in place of the real one."""
    parser = argparse.ArgumentParser(
        description="Check that the running system refuses requests beyond a user's role. It changes the case: "
                    f"run it on freshly loaded data ({RELOAD}).")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="the .env to read (default: .env)")
    parser.add_argument("--gateway", help="the gateway's URL (default: http://127.0.0.1:<GATEWAY_HOST_PORT>)")
    parser.add_argument("--case-service", help="the case service's URL on this machine "
                                               "(default: http://127.0.0.1:<CASE_HOST_PORT>)")
    parser.add_argument("--case", default="case_1042", help="the pending_review case to use (default: case_1042)")
    parser.add_argument("--timeout", type=float, default=300,
                        help="seconds to wait for each review to await a decision (default: 300)")
    parser.add_argument("--out", type=Path, help="also write the results to this file, as a Markdown table")
    args = parser.parse_args(argv)

    if not args.env.is_file():
        print(f"Couldn't start: no {args.env}. Copy .env.example to .env and fill it in "
              "(docs/local-testing.md, step 1).", file=sys.stderr)
        return 2
    env = read_env(args.env)
    gateway_url = (args.gateway or f"http://127.0.0.1:{env.get('GATEWAY_HOST_PORT') or 8080}").rstrip("/")
    case_url = (args.case_service or f"http://127.0.0.1:{env.get('CASE_HOST_PORT') or 8001}").rstrip("/")
    hidden = [env.get(name, "") for name in SECRET_VARIABLES]

    def say(text: str) -> None:
        print(redact(text, hidden), flush=True)

    say(f"This changes {args.case}: the analyst escalates it, then the senior analyst approves it, as steps 5 and "
        f"12 of docs/local-testing.md do.\nRun it on freshly loaded data: {RELOAD}.\n")
    with httpx.Client(base_url=gateway_url, timeout=30, transport=transport) as gateway, \
            httpx.Client(base_url=case_url, timeout=30, transport=transport) as case_service:
        run = Run(gateway, case_service, env, args.case, args.timeout, say=say)
        hidden = run.hidden                       # from now on, the tokens are blanked out too
        try:
            run.start()
        except CantStart as error:
            print(redact(f"Couldn't start: {error}", hidden), file=sys.stderr)
            return 2
        results = run.run()
        status = run.final_status()

    say(f"\n{args.case} is now {status or 'in an unknown status'}. Load the data again before the next run, or "
        f"before the steps in docs/local-testing.md: {RELOAD}.")
    if args.out:
        args.out.write_text(redact(markdown(results, run.setup, gateway_url, case_url, args.case,
                                            datetime.now(timezone.utc)), hidden), encoding="utf-8")
        say(f"Wrote {args.out}")
    say(summary(results))
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
