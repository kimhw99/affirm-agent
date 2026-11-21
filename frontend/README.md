# Review app

The analysts' app: log in, pick a case from the queue, read the agent's memo with its evidence, and decide. It covers BEAC-15 (login and the case queue), BEAC-16 (the memo and evidence view), BEAC-17 (the decision form) and BEAC-20 (the override rate). It talks only to the gateway, through `/api` ([docs/auth.md](../docs/auth.md)).

## Run it

With Docker Compose, from the repo root, run `docker compose up -d` and open http://localhost:3000 (`APP_HOST_PORT`). nginx serves the built app and forwards `/api` to the gateway. The first time, fill in `.env` and load the golden cases as in [docs/local-testing.md](../docs/local-testing.md), which also lists what to check in the browser.

To work on the app with hot reload while the backend runs in Docker Compose:

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173; /api goes to the gateway at localhost:8080
```

## Test and build

```bash
npm test           # the pages, with a mocked API
npm run build      # into dist/
```

The tests use the agent's example review (`services/agent/examples/review_output_case_1042.json`) and `case_1042` from `contracts/examples/`, so a change to those formats shows up here.

## Pages

| Route | Page |
|---|---|
| `/login` | Log in (`POST /api/auth/login`) |
| `/` | The case queue. An analyst starts on `pending_review` cases and a senior analyst on `escalated` ones |
| `/cases/:caseId` | One case. If the user may decide it, the page starts its review or picks up the existing one (`POST /api/reviews`), waits while the agent works, then shows the memo, the checks, the evidence behind each ID and the decision form, with only the decisions the user may choose. Otherwise it starts no review: it says why, for example that only a senior analyst can decide an escalated case, and shows the case's decisions. A `docs_requested` case also has a form for the documents the merchant sent (`POST /api/cases/{case_id}/documents`): adding one brings the case back to Pending review, and its new review starts |
| `/metrics` | The override rate (`GET /api/metrics/override-rate`), for senior analysts only. Only they get the Metrics link in the top bar; anyone else who opens the page gets a notice, and no request is sent |

## Logs

`src/log.js` writes what the app does to the browser's console: logins, reviews started or failed, decisions saved or failed, and each API call that fails, with the `request_id` the gateway answered with, to find the services' lines for it ([docs/local-testing.md](../docs/local-testing.md#reading-the-logs)). It never writes a password, a token or a note.

## Roles

What each role may do is the Roles table in [docs/auth.md](../docs/auth.md). The backend enforces it; the app follows it (`src/format.js`: `canDecide`, `allowedDecisions`, `METRICS_ROLES`), so each role only sees the pages and actions it can use. When the two disagree, the backend wins: a 403 shows "You don't have permission to do this." and keeps the user logged in, while a 401 means the session is over and goes back to the login page.
