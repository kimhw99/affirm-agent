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
| `/` | The case queue. An analyst starts on `pending_review` cases and a senior analyst on `escalated` ones. A senior analyst also sees the override rate (`GET /api/metrics/override-rate`); the gateway answers 403 to an analyst, so the page leaves it out |
| `/cases/:caseId` | One case. If it can take a decision, the page starts its review or picks up the existing one (`POST /api/reviews`), waits while the agent works, then shows the memo, the checks, the evidence behind each ID and the decision form. A decided case shows its decisions instead |
