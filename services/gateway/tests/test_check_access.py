"""scripts/check_access.py without a running system: reading .env, the tokens it forges (against
the real gateway), waiting for a review, the results it prints and writes, and a whole run
against a fake system that follows the Roles table (docs/auth.md), or breaks one of its rules."""

import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from app.main import Settings, create_app

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("check_access", REPO / "scripts" / "check_access.py")
check_access = importlib.util.module_from_spec(_spec)
sys.modules["check_access"] = check_access
_spec.loader.exec_module(check_access)

SECRET = "test-secret-that-is-long-enough-for-hs256"
ENV = {"DEMO_ANALYST_PASSWORD": "analyst-demo-password", "DEMO_SENIOR_PASSWORD": "senior-demo-password",
       "CASE_SERVICE_TOKEN": "the-case-service-token", "JWT_SECRET": SECRET,
       "GATEWAY_HOST_PORT": "18080", "CASE_HOST_PORT": "18001"}
USERS = {"analyst": ("u_analyst_01", "analyst"), "senior": ("u_senior_01", "senior_analyst")}
NOW = int(time.time())
CLAIMS = {"sub": "u_analyst_01", "role": "analyst", "name": "Demo Analyst", "iat": NOW, "exp": NOW + 600}


def answer(status: int, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail})


def write_env(folder: Path, values: dict[str, str] = ENV) -> Path:
    path = folder / ".env"
    path.write_text("# a comment\n\n" + "".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    return path


# ---------- .env ----------

def test_read_env_reads_the_usual_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_text("\n".join([
        "# Lists every variable",
        "",
        "PLAIN=value",
        "EMPTY=",
        "SPACED = spaced value  ",
        "EQUALS=a=b=c",
        "HASH=pa#ss",
        "COMMENTED=value # a comment",
        "DOUBLE=\"quoted # not a comment\"",
        "SINGLE='single quoted'",
        "QUOTED_THEN_COMMENT=\"abc\" # a comment",
        "export EXPORTED=yes",
        "# COMMENTED_OUT=no",
        "no equals sign here",
    ]) + "\r\n", encoding="utf-8")
    assert check_access.read_env(path) == {
        "PLAIN": "value", "EMPTY": "", "SPACED": "spaced value", "EQUALS": "a=b=c", "HASH": "pa#ss",
        "COMMENTED": "value", "DOUBLE": "quoted # not a comment", "SINGLE": "single quoted",
        "QUOTED_THEN_COMMENT": "abc", "EXPORTED": "yes"}


def test_read_env_reads_env_example():
    values = check_access.read_env(REPO / ".env.example")
    assert set(check_access.SECRET_VARIABLES) <= set(values)
    assert values["GATEWAY_HOST_PORT"] == "8080" and values["CASE_HOST_PORT"] == "8001"


# ---------- The tokens it forges ----------

def test_the_expired_token_is_good_but_for_its_date():
    token = check_access.expired_token(CLAIMS, SECRET, NOW)
    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(token, SECRET, algorithms=["HS256"])
    claims = jwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_exp": False})
    assert (claims["sub"], claims["role"]) == ("u_analyst_01", "analyst") and claims["exp"] < NOW


def test_the_other_key_token_is_in_date_but_not_signed_with_the_secret():
    token = check_access.other_key_token(CLAIMS, NOW)
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, SECRET, algorithms=["HS256"])
    assert check_access.claims_of(token)["exp"] > NOW


def test_the_unsigned_token_has_alg_none_and_no_signature():
    token = check_access.unsigned_token(CLAIMS, NOW)
    assert jwt.get_unverified_header(token)["alg"] == "none" and token.endswith(".")
    with pytest.raises(jwt.InvalidAlgorithmError):
        jwt.decode(token, SECRET, algorithms=["HS256"])


@pytest.fixture
def gateway():
    """The real gateway, with fake services behind it, and the requests that reached them."""
    seen: list[httpx.Request] = []

    def service(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    settings = Settings(jwt_secret=SECRET, service_token="service-token",
                        urls={"auth": "http://auth", "cases": "http://case", "metrics": "http://case",
                              "reviews": "http://agent"})
    with TestClient(create_app(settings, httpx.MockTransport(service))) as client:
        yield client, seen


@pytest.mark.parametrize("forge, said", [
    (lambda: check_access.expired_token(CLAIMS, SECRET, NOW), "the session has expired; log in again"),
    (lambda: check_access.other_key_token(CLAIMS, NOW), "invalid token"),
    (lambda: check_access.unsigned_token(CLAIMS, NOW), "invalid token"),
])
def test_the_gateway_refuses_each_forged_token_for_the_reason_the_check_expects(gateway, forge, said):
    client, seen = gateway
    response = client.get("/cases", headers=check_access.bearer(forge()))
    assert (response.status_code, response.json()["detail"]) == (401, said)
    assert seen == []


@pytest.mark.parametrize("forged", [check_access.FORGED_USER, check_access.UNDERSCORED_USER])
def test_the_gateway_sends_the_role_from_the_jwt_whatever_the_client_says(gateway, forged):
    client, seen = gateway
    analyst = check_access.bearer(jwt.encode(CLAIMS, SECRET, algorithm="HS256"))
    assert client.get("/metrics/override-rate", headers=analyst | forged).status_code == 403
    client.post("/reviews", json={"case_id": "case_1042"}, headers=analyst | forged)
    (request,) = seen
    assert (request.headers["x-user-id"], request.headers["x-user-role"]) == ("u_analyst_01", "analyst")


# ---------- What the service said, and the verdict ----------

def test_reason_reads_the_detail_a_validation_error_or_the_status_line():
    assert check_access.reason(answer(401, "log in first")) == "log in first"
    errors = [{"loc": ["body", "case"], "msg": "Extra inputs are not permitted", "input": {"case_id": "x"}},
              {"loc": ["body", "other"], "msg": "Extra inputs are not permitted"}]
    validation = httpx.Response(422, json={"detail": errors})
    assert check_access.reason(validation) == "body.case: Extra inputs are not permitted (and 1 more)"
    assert check_access.reason(httpx.Response(502, text="<html>Bad Gateway</html>")) == "Bad Gateway"
    assert check_access.reason(httpx.Response(200, json=[{"case_id": "case_1042"}])) == "OK"


def check(expected=401, reason_has="", send=lambda: answer(401, "log in first")):
    return check_access.Check("A check", "nobody", "GET /cases", expected, "Because.", send, reason_has)


def test_a_check_passes_on_the_status_and_where_asked_the_reason():
    assert check_access.run_check(check()).passed
    assert check_access.run_check(check(reason_has="LOG IN")).passed
    assert not check_access.run_check(check(expected=403)).passed
    assert not check_access.run_check(check(reason_has="expired")).passed


def test_no_answer_is_a_failure_and_says_so():
    def unreachable():
        raise httpx.ConnectError("connection refused")
    result = check_access.run_check(check(send=unreachable))
    assert (result.passed, result.status, result.reason) == (False, None, "no answer (ConnectError)")


# ---------- Waiting for a review ----------

def run_against(system, *, timeout=300, clock=time.monotonic, sleep=lambda seconds: None, env=ENV):
    """A Run against a fake gateway (system answers each request), already logged in."""
    client = httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(system))
    run = check_access.Run(client, client, env, "case_1042", timeout, say=lambda text: None, clock=clock,
                           sleep=sleep)
    run.tokens = {"analyst": "analyst-token", "senior": "senior-token"}
    return run


def reviews(*statuses, review_id="rv_1"):
    """A fake agent: POST /reviews answers the first status, each GET the next."""
    answers = iter(statuses)

    def system(request: httpx.Request) -> httpx.Response:
        body = {"review_id": review_id, "status": next(answers)}
        if body["status"] == "failed":
            body["error"] = "RuntimeError: the graph broke"
        return httpx.Response(202 if request.method == "POST" else 200, json=body)
    return system


def test_a_review_is_polled_until_it_awaits_the_decision():
    naps = []
    run = run_against(reviews("running", "running", "awaiting_decision"), sleep=naps.append)
    assert run.review_ready("analyst") == "rv_1"
    assert naps == [check_access.POLL_SECONDS] * 2


def test_waiting_for_a_review_stops_at_the_timeout():
    ticks = iter(range(0, 1000, 10))         # each look at the clock is 10 seconds later
    run = run_against(reviews(*["running"] * 50), timeout=30, clock=lambda: next(ticks))
    with pytest.raises(check_access.SetupFailed, match="still running after 30 s"):
        run.review_ready("analyst")


@pytest.mark.parametrize("status, says", [("done", "docker compose restart agent"),
                                          ("failed", "RuntimeError: the graph broke")])
def test_a_review_that_wont_await_a_decision_stops_the_run(status, says):
    with pytest.raises(check_access.SetupFailed, match=says):
        run_against(reviews("running", status)).review_ready("analyst")


# ---------- A whole run, against a fake system ----------

class FakeSystem:
    """The gateway, auth, the agent and the case service as the script sees them, following the
    Roles table, except for the rules in `broken`."""

    def __init__(self, status="pending_review", broken=()):
        self.status, self.broken, self.reviews = status, set(broken), 0
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.port == int(ENV["CASE_HOST_PORT"]):
            if request.headers.get("authorization") != f"Bearer {ENV['CASE_SERVICE_TOKEN']}":
                return answer(401, "a valid service token is required")
            return httpx.Response(200, json={"case_id": "case_1042", "status": self.status})
        return self.gateway(request, request.url.path, json.loads(request.content or b"null"))

    def gateway(self, request, path, body):
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/auth/login":
            if body["password"] != ENV[check_access.PASSWORDS[body["username"]]]:
                return answer(401, "wrong username or password")
            sub, role = USERS[body["username"]]
            claims = {"sub": sub, "role": role, "name": body["username"], "iat": NOW, "exp": NOW + 600}
            return httpx.Response(200, json={"access_token": jwt.encode(claims, SECRET, algorithm="HS256")})
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not token:
            return answer(401, "log in first")
        try:
            role = jwt.decode(token, SECRET, algorithms=["HS256"])["role"]
        except jwt.ExpiredSignatureError:
            return answer(401, "the session has expired; log in again")
        except jwt.InvalidTokenError:
            return answer(401, "invalid token")
        if "trusts X-User-Role" in self.broken:
            role = request.headers.get("x-user-role", role)
        if path == "/metrics/override-rate":
            return httpx.Response(200, json={}) if role == "senior_analyst" else \
                answer(403, "/metrics is only for senior_analyst")
        if path.endswith("/decisions"):
            return answer(405, f"POST isn't allowed on {path}")
        if path.endswith("/documents"):
            return answer(409, f"case case_1042 is {self.status}; only a docs_requested case takes new documents")
        if path.startswith("/cases"):
            return httpx.Response(200, json={"case_id": "case_1042", "status": self.status, "flag_reasons": ["X"]})
        if path == "/reviews" and set(body) != {"case_id"}:
            return httpx.Response(422, json={"detail": [{"loc": ["body", "case"], "msg": "Extra inputs are not permitted"}]})
        decision = body.get("decision")
        refused = self.refuse(role, "decide" if decision else "review", decision)
        if refused:
            return refused
        if "agent down" in self.broken:
            return answer(502, "the agent service isn't reachable")
        if decision and "decisions fail" in self.broken:
            return answer(502, "the case service refused the decision")
        if decision:
            self.status = {"escalate": "escalated", "approve": "approved"}[decision]
            return httpx.Response(200, json={"status": "done"})
        self.reviews += 1
        if "keeps an old review" in self.broken and self.reviews == 1:
            return httpx.Response(202, json={"review_id": "rv_old", "created": False, "status": "done"})
        return httpx.Response(202, json={"review_id": f"rv_{self.reviews}", "status": "awaiting_decision"})

    def refuse(self, role, action, decision):
        if self.status not in ("pending_review", "escalated"):
            return answer(409, f"case_1042 is {self.status}, so it can't be reviewed or decided")
        if self.status == "escalated" and role != "senior_analyst":
            return answer(403, f"only senior_analyst can {action} a case that is escalated")
        if self.status == "escalated" and decision == "escalate":
            return answer(409, "case_1042 is already escalated, so decide something other than escalate")
        return None


def run_main(tmp_path, system, capsys, env=ENV):
    out = tmp_path / "results.md"
    code = check_access.main(["--env", str(write_env(tmp_path, env)), "--out", str(out)],
                             transport=httpx.MockTransport(system))
    printed = capsys.readouterr()
    return code, printed.out, printed.err, out.read_text(encoding="utf-8") if out.exists() else ""


def secrets_in(*texts: str) -> list[str]:
    return [value for value in check_access.SECRET_VARIABLES for text in texts if ENV[value] in text]


def test_a_system_that_follows_the_roles_passes_every_check(tmp_path, capsys):
    system = FakeSystem()
    code, out, err, table = run_main(tmp_path, system, capsys)
    assert code == 0 and err == ""
    assert out.rstrip().splitlines()[-1] == "20 of 20 passed"
    assert "This changes case_1042" in out.splitlines()[0]
    assert "case_1042 is now approved" in out
    rows = [line for line in table.splitlines() if line.startswith("| ") and line[2].isdigit()]
    assert len(rows) == 20 and all("| PASS |" in row for row in rows)
    assert "analyst decided escalate on review rv_1: case_1042 is now escalated" in table
    assert "senior decided approve on review rv_2: case_1042 is now approved" in table
    assert secrets_in(out, err, table) == [] and "eyJ" not in out + table      # no secret and no JWT
    assert system.status == "approved"


def test_a_broken_rule_is_a_failure_with_what_the_service_said(tmp_path, capsys):
    code, out, _, table = run_main(tmp_path, FakeSystem(broken={"trusts X-User-Role"}), capsys)
    assert code == 1
    assert out.rstrip().splitlines()[-1] == "18 of 20 passed"
    failed = [line for line in table.splitlines() if "| FAIL |" in line]
    assert [line.split(" | ")[1] for line in failed] == ["The override rate, with a forged X-User-Role",
                                                         "A review of an escalated case, with a forged X-User-Role"]
    assert "| 403 (only for senior_analyst) | 200 | FAIL |" in failed[0]
    assert "| 403 (only senior_analyst) | 202 | FAIL |" in failed[1]


def test_when_a_step_fails_the_checks_after_it_dont_run(tmp_path, capsys):
    code, out, _, table = run_main(tmp_path, FakeSystem(broken={"decisions fail"}), capsys)
    assert code == 1
    assert out.rstrip().splitlines()[-1] == "13 of 20 passed"
    assert table.count("| NOT RUN |") == 7
    assert "the analyst escalates case_1042 failed (analyst's escalate on review rv_1 got 502" in table


@pytest.mark.parametrize("broken, says", [
    ("keeps an old review", "review rv_old, which is done: it still has a review from before"),
    ("agent down", "analyst couldn't start a review: 502"),
])
def test_an_agent_that_cant_start_a_fresh_review_stops_it_before_any_check(tmp_path, capsys, broken, says):
    code, out, err, table = run_main(tmp_path, FakeSystem(broken={broken}), capsys)
    assert code == 2 and table == "" and says in err
    assert "PASS" not in out and "FAIL" not in out


def test_a_case_that_isnt_freshly_loaded_stops_it_before_anything_changes(tmp_path, capsys):
    system = FakeSystem(status="approved")
    code, _, err, table = run_main(tmp_path, system, capsys)
    assert code == 2 and table == ""
    assert "case_1042 is approved, not pending_review" in err and "seed.py --reset" in err
    assert [request.method for request in system.requests] == ["GET", "POST", "POST", "GET"]   # no change


def test_a_failed_login_names_the_variable_and_not_the_password(tmp_path, capsys):
    code, _, err, _ = run_main(tmp_path, FakeSystem(), capsys, env=ENV | {"DEMO_SENIOR_PASSWORD": "a-wrong-one"})
    assert code == 2
    assert "logging in as senior got 401" in err and "DEMO_SENIOR_PASSWORD" in err
    assert "a-wrong-one" not in err and secrets_in(err) == []


def test_a_missing_variable_is_named_without_any_value(tmp_path, capsys):
    env = {key: value for key, value in ENV.items() if key != "JWT_SECRET"}
    code, _, err, _ = run_main(tmp_path, FakeSystem(), capsys, env=env)
    assert code == 2 and "JWT_SECRET empty or missing" in err and secrets_in(err) == []


# ---------- What it prints and writes ----------

def test_redact_blanks_out_every_secret_value():
    text = "token abc123456 and the-case-service-token, but not abc"
    assert check_access.redact(text, ["the-case-service-token", "abc123456", "abc", ""]) == \
        "token [redacted] and [redacted], but not abc"


def test_the_table_has_one_row_per_check_with_cells_that_cant_break_it():
    tricky = check_access.Check("A | check", "analyst", "GET /cases\nwith a newline", 403, "Roles.",
                                lambda: answer(403, "x"), reason_has="only")
    results = [check_access.Result(tricky, 403, "only | senior", True),
               check_access.not_run(tricky, "not run: the analyst escalates case_1042 failed (502)")]
    table = check_access.markdown(results, ["analyst decided escalate"], "http://127.0.0.1:8080",
                                  "http://127.0.0.1:8001", "case_1042", datetime(2026, 11, 21, 6, 0, tzinfo=timezone.utc))
    lines = table.splitlines()
    assert lines[0] == "# Access checks" and "on 2026-11-21 06:00 UTC: **1 of 2 passed**" in lines[2]
    assert "- analyst decided escalate" in lines
    assert lines[-2] == ("| 1 | A \\| check | analyst | GET /cases with a newline | 403 (only) | 403 | PASS | "
                         "only \\| senior | Roles. |")
    assert "| - | NOT RUN | not run: the analyst escalates case_1042 failed (502) |" in lines[-1]


def test_the_console_line_shows_the_reason_only_for_a_check_that_didnt_pass():
    passed = check_access.Result(check(), 401, "log in first", True)
    failed = check_access.Result(check(), 200, "OK", False)
    assert check_access.console_line(1, passed) == "  1  PASS     401 (expected 401)  nobody   A check"
    assert check_access.console_line(2, failed).splitlines() == [
        "  2  FAIL     200 (expected 401)  nobody   A check", "        OK"]
    assert check_access.summary([passed, failed]) == "1 of 2 passed"
