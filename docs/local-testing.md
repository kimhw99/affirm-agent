# Running and testing the app locally

How to run the whole system on one machine with Docker Compose, check in the browser that its parts work together, and see what each part did. To test each part on its own, see [Automated tests](#automated-tests).

## What you need

- Docker Desktop, or Docker Engine with Compose v2, running.
- The code to test, usually the latest `main`: `git checkout main`, then `git pull`.
- Optionally, an OpenAI API key, for real reviews (step 1).

## 1. Fill in `.env` (once)

From the repo root:

```bash
cp .env.example .env
```

Then open `.env` and set:

| Variable | Set it to |
|---|---|
| `POSTGRES_PASSWORD`, `AUTH_DB_PASSWORD`, `CASE_DB_PASSWORD`, `AGENT_DB_PASSWORD` | Any four passwords. The database sets them up the first time it starts, so don't change them afterwards ([Troubleshooting](#troubleshooting)) |
| `CASE_SERVICE_TOKEN` | A long random string, for example from the command in the next row |
| `JWT_SECRET` | At least 32 characters: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `DEMO_ANALYST_PASSWORD`, `DEMO_SENIOR_PASSWORD` | The passwords you'll log in with, as `analyst` and as `senior` |
| `OPENAI_API_KEY` | Optional. Without it the app still runs from end to end, but the workers can't check anything, so the memo recommends Insufficient evidence |

Leave the rest as they are. `.env` is git-ignored; never commit it.

## 2. Start the app

```bash
docker compose up -d --build --wait            # the first build takes a few minutes
docker compose run --rm case python seed.py    # loads the golden cases from data/golden into case_db
```

`--wait` returns once all six services (`db`, `case`, `auth`, `agent`, `gateway`, `frontend`) are healthy; `docker compose ps` shows them. Then open http://localhost:3000.

## 3. Check it in the browser

These steps take case_1042 through an analyst's decision and then a senior analyst's, so they go through every part: the review app, the gateway, auth, the case service, the agent and the database. Log in with the demo passwords from `.env`.

| # | Do | You should see |
|---|---|---|
| 1 | Log in as `analyst` with a wrong password | "Wrong username or password." |
| 2 | Log in as `analyst` with the right one | "Demo Analyst · Analyst" at the top, with no Metrics link (only a senior analyst sees the override rate), and the case queue, on Pending review, with case_1042 |
| 3 | Open case_1042 | "The agent is reviewing this case. This page updates by itself.", then the agent's memo, the checks and the decision form. Without an OpenAI key this takes a few seconds, and the memo recommends Insufficient evidence, with both flags (DBA_UNVERIFIED, TIN_MISMATCH) unresolved; with a key it takes longer |
| 4 | Click an evidence ID (R1, D1, …) in the memo or the checks | The panel on the right: the evidence's source, type, date and excerpt. This needs an OpenAI key: without one, nothing cites evidence, so there's no ID to click |
| 5 | Choose Escalate, write a note, and click Submit decision | "Decision saved" and "The case is now Escalated." If the recommendation is anything but Escalate or Insufficient evidence, a warning first says this counts as an override |
| 6 | Click ← Case queue | case_1042 has left Pending review; choose Escalated under Status to find it |
| 7 | Open case_1042 from there | "This case is Escalated. Only a senior analyst can decide it.", and the analyst's decision. No review starts |
| 8 | Reload the page, then click Log out | Still logged in after the reload; Log out goes back to the login page |
| 9 | Log in as `senior` | "Demo Senior Analyst · Senior analyst" and a Metrics link at the top, and the queue on Escalated, with case_1042 |
| 10 | Click Metrics | The override rate, which counts the analyst's decision: "… of 1 decisions went against the agent's recommendation." |
| 11 | Click ← Case queue, then open case_1042 | A new review runs, because the case has changed, then the memo and the decision form. The form has no Escalate, since the case is escalated already |
| 12 | Choose Approve, write a note, and click Submit decision | Unless the recommendation is Recommend clear, a warning first says this counts as an override. Then "Decision saved", "against the agent's recommendation" and "The case is now Approved." |
| 13 | Click ← Case queue, then choose All cases under Status | case_1042 is Approved |
| 14 | Open case_1042 | "This case is Approved, so it can't take a new decision.", and both decisions, by `u_analyst_01` and `u_senior_01`, each override marked "(override)" |
| 15 | Click Metrics | The override rate counts both decisions |

Without an OpenAI key, Escalate in step 5 agrees with the recommendation and Approve in step 12 doesn't, so in step 15 the override rate is 50%: 1 of 2 decisions.

Then the merchant's documents: a case the analyst sends back for documents returns to review once they arrive. [Start over](#5-start-over-stop-or-update) first, so case_1042 is pending again.

| # | Do | You should see |
|---|---|---|
| 16 | Log in as `analyst`, open case_1042, choose Request documents, write a note, and click Submit decision | "Decision saved" and "The case is now Docs requested." |
| 17 | Click ← Case queue, choose Docs requested under Status, and open case_1042 | "Add the documents the merchant sent", with a reminder to enter only a tax ID's last 4 digits, and the decision that asked for them. No review starts |
| 18 | Choose IRS letter 147C, fill in Name (NORTHLINE BICYCLE WORKS LLC) and EIN, last 4 digits (4567), and click Add the document | "Adding…", then "Added D2: IRS letter 147C (the EIN and the name on file). The case is back in Pending review, so a new review starts.", then the new review's memo and decision form. With an OpenAI key, the memo or the evidence panel can cite D2 |
| 19 | Click ← Case queue | case_1042 is back in Pending review |

To see what was saved: `docker compose exec db psql -U case_svc -d case_db -c "SELECT doc_id, type, extracted, confidence FROM documents WHERE case_id = 'case_1042'"`, and the audit log query in section 4, which shows the `documents_received` entry.

To go through the steps again, [start over](#5-start-over-stop-or-update) first. For now `data/golden` holds only case_1042; once it holds more cases, open a few: each gets its own review.

## 4. See what each part did

While someone clicks through, each part's owner can watch theirs. `-f` keeps following a log; Ctrl+C stops it.

| To see | Run |
|---|---|
| Whether each service is up and healthy | `docker compose ps` |
| The requests the agent gets, and its warnings and errors. Without an API key, each worker task logs `RuntimeError: OPENAI_API_KEY is not set`, as expected | `docker compose logs -f agent` |
| Each request to the case service, from the gateway and from the agent; with an API key, the workers' lookups too | `docker compose logs -f case` |
| Logins and the requests the gateway forwards | `docker compose logs -f auth gateway` |
| The saved decisions | `docker compose exec db psql -U case_svc -d case_db -c "SELECT review_id, decision, analyst_id, overridden, decided_at FROM decisions"` |
| Every status change (the audit log) | `docker compose exec db psql -U case_svc -d case_db -c "SELECT case_id, actor_id, action, from_status, to_status, created_at FROM audit_log"` |

## 5. Start over, stop or update

| To | Run |
|---|---|
| Start over: delete every decision and the audit log, load the golden cases again, and clear the agent's reviews | `docker compose run --rm case python seed.py --reset`, then `docker compose restart agent` |
| Stop, and keep the data | `docker compose down`; `docker compose up -d --wait` starts it again |
| Stop, and delete the data in all three databases | `docker compose down -v`; after the next start, run `seed.py` again |
| Run new code after a `git pull` | `docker compose up -d --build --wait`; if `data/golden` changed, run `seed.py` again |

The agent keeps its reviews in memory for now ([services/agent/README.md](../services/agent/README.md)). Restarting it, or `docker compose down`, drops them, and a case that can still take a decision gets a new review when it's next opened. Without the restart after `--reset`, the agent would still remember the old reviews, and case_1042 would open on its finished review instead of a new one. The restart takes a few seconds; `docker compose ps` shows the agent healthy again.

## Troubleshooting

| What you see | Why, and what to do |
|---|---|
| `required variable … is missing a value` | That variable is empty in `.env`; fill it in |
| `dependency failed to start: container …-auth-1 exited (3)`, or the same for another service | That service couldn't start; `docker compose logs auth` (or that service) says why. The two usual reasons are in the next rows |
| The log says `JWT_SECRET must be at least 32 characters` | Make `JWT_SECRET` in `.env` longer, then run `up` again |
| The log says `password authentication failed` | A database password in `.env` changed after the database's first start, which set up the old one. Put the old one back, or start over with `docker compose down -v` (deletes all data), `up` and `seed.py` |
| `port is already allocated` or `address already in use` | Another program uses that port. Change `APP_HOST_PORT` (3000), `GATEWAY_HOST_PORT` (8080), `CASE_HOST_PORT` (8001) or `POSTGRES_HOST_PORT` (55432) in `.env`. With a new `APP_HOST_PORT`, open the app on that port |
| The queue says "No cases here." | The golden cases aren't loaded, for example after `down -v`: run `seed.py` |
| `seed.py` says "Nothing was loaded: these cases have decisions or audit log entries" | It doesn't replace a case that has decisions. [Start over](#5-start-over-stop-or-update), which deletes them |
| Every memo recommends Insufficient evidence and cites no evidence | `OPENAI_API_KEY` is empty or wrong; `docker compose logs agent` shows the warning or the error. Fix it in `.env`, then run `docker compose up -d` |
| "The review failed: …" | The agent hit an error; see `docker compose logs agent`. Then click Start it again |
| A change to `.env` has no effect | Run `docker compose up -d`, which recreates the services whose settings changed; `docker compose restart` keeps the old settings |
| New code from `git pull` doesn't show | Rebuild: `docker compose up -d --build --wait` |

## Automated tests

Each part has its own tests, with fakes in place of the database, the other services and the LLM, so they need none of the above. With Python 3.12 and Node, as in the containers, from the repo root:

```bash
python3 -m venv .venv && source .venv/bin/activate    # once; on Windows: .venv\Scripts\activate
pip install -r services/case/requirements.txt -r services/auth/requirements.txt \
    -r services/gateway/requirements.txt -r services/agent/requirements.txt
pytest services/case/tests        # one service per run: each has its own app package
pytest services/auth/tests
pytest services/gateway/tests
pytest services/agent/tests
cd frontend && npm install && npm test
```
