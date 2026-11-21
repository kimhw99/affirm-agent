# Auth

How users log in, what the JWT carries, and how the gateway tells the backend services who is acting.

- Login: [services/auth/](../services/auth/)
- The checks on every request: [services/gateway/](../services/gateway/)
- The formats (roles, login, the JWT's claims): [contracts/kyb_contracts/auth.py](../contracts/kyb_contracts/auth.py)

## Roles

| Role | Who | The queue starts on |
|---|---|---|
| `analyst` | Reviews the cases waiting for review | `pending_review` |
| `senior_analyst` | Takes the escalated cases, and sees the override rate | `escalated` |

What each role may do. The backend enforces every rule, whatever the review app shows. The review app follows the same table (`frontend/src/format.js`), so each role only sees what it can use; a change here needs the same change there:

| Action | `analyst` | `senior_analyst` | Checked by |
|---|---|---|---|
| See the queue, open a case, see its decisions | Yes | Yes | The gateway lets both roles through (`services/gateway/app/main.py`, `verify`) |
| `GET /metrics/override-rate` | 403 | Yes | The gateway: the `metrics` route's `roles` (`services/gateway/app/main.py`, `ROUTES`) |
| A `pending_review` case: start a review, decide it (`approve`, `request_docs`, `escalate`, `deny`) | Yes | Yes | The agent service (`services/agent/app/api/reviews.py`, `MAY_ACT`) |
| An `escalated` case: start a review, decide it | 403 | Yes, but not `escalate` (409) | The agent service (`reviews.py`, `MAY_ACT` and `REFUSED_DECISIONS`) |
| An `approved`, `denied` or `docs_requested` case: start a review or decide it | 409 | 409 | The agent service (`reviews.py`). The case service refuses a decision on such a case too (409, `services/case/app/store.py`) |
| A `docs_requested` case: add the documents the merchant sent (`POST /cases/{case_id}/documents`), which sends it back to `pending_review` | Yes | Yes | The gateway lets both roles through this one write (`services/gateway/app/main.py`, `post_paths`); the case service checks the user (403) and the status: any other case is 409 (`services/case/app/main.py` and `store.py`) |

`POST /reviews` takes only `{"case_id"}` (422 for anything else), and the agent service reads the case from the case service, so a review is always of the stored case. It reads the case's status each time it checks a rule: when a review starts, and again before it resumes the graph with a decision, so a refused decision changes nothing. If the case service still refuses the decision when the graph sends it (step 11), for example because another review decided the case first, `POST /reviews/{review_id}/decision` answers 409 (502 for any other error) and the review fails: `GET /reviews/{review_id}` says `failed`, with the reason, and starting a review of the case again runs a new one.

The queue each role starts on is the review app's default (BEAC-15); either role can choose another status.

## Logging in

`POST /auth/login` with `{"username", "password"}` returns `{access_token, token_type, expires_in, user}`. The review app keeps the token and sends it as `Authorization: Bearer <token>` with every request. `GET /auth/me` returns the user a token belongs to.

A wrong username and a wrong password get the same 401, so the answer doesn't reveal which one was wrong. Passwords are stored as scrypt hashes in `auth_db.users`, never in plain text, and never returned.

## The JWT

Signed with HS256 and `JWT_SECRET` (at least 32 characters), which the auth service and the gateway share. Its claims (`TokenClaims`):

| Claim | Meaning |
|---|---|
| `sub` | The user's ID, such as `u_analyst_01`. It becomes `analyst_id` in the `DecisionRecord` |
| `role` | `analyst` or `senior_analyst` |
| `name` | The display name, for the review app |
| `iat`, `exp` | When it was issued and when it expires, in Unix seconds. A login lasts `JWT_TTL_MINUTES`, 480 by default |

## What the gateway lets through

The browser loads the review app from `http://localhost:3000` (`APP_HOST_PORT`), and the app's nginx forwards every `/api/...` request to the gateway, without the `/api` prefix. The gateway, at `http://localhost:8080` (`GATEWAY_HOST_PORT`), is the only backend service the browser reaches.

| Path | Goes to | Needs a JWT | Methods |
|---|---|---|---|
| `/auth/...` | Auth service | No: the auth service checks its own credentials | GET, POST |
| `/cases/...` | Case service | Yes | GET, and POST for `/cases/{case_id}/documents` only |
| `/metrics/...` | Case service | Yes, with the `senior_analyst` role | GET |
| `/reviews/...` | Agent service | Yes | GET, POST |

For `/cases`, `/metrics` and `/reviews`, the gateway:

1. Verifies the JWT: 401 if it's missing, invalid or expired, and 403 if its role isn't one of the two above, or for `/metrics`, isn't `senior_analyst`.
2. Drops any `X-User-*` header the client sent, and sets `X-User-Id` (the `sub`) and `X-User-Role`.
3. Doesn't pass the JWT on. Requests to the case service carry the service token (`CASE_SERVICE_TOKEN`) instead.

Only reads reach the case service, and one write: `POST /cases/{case_id}/documents`, where an analyst adds a document the merchant sent for a `docs_requested` case. `POST /cases/{case_id}/decisions` is for the agent service, so a decision always goes through its review (`POST /reviews/{review_id}/decision`); from the browser it's 405, like any other write. Any other path is 404.

The backend services can trust `X-User-Id` and `X-User-Role` because the gateway is the only way to reach them: in Docker Compose the auth and agent services aren't published, and the case service needs the service token.

## Service to service

The agent service calls the case service with `CASE_SERVICE_TOKEN` ([case-service-endpoints.md](case-service-endpoints.md)). It's a shared secret for now. A service JWT can replace it later without changing the agent's side.

## Demo users

When it starts, the auth service creates these users, or updates them to match `.env`, if their passwords are set:

| Username | Role | Password |
|---|---|---|
| `analyst` | `analyst` | `DEMO_ANALYST_PASSWORD` |
| `senior` | `senior_analyst` | `DEMO_SENIOR_PASSWORD` |

## Try it

With the stack running and the golden cases loaded ([database.md](database.md), Setting up):

```bash
TOKEN=$(curl -s localhost:8080/auth/login -H 'Content-Type: application/json' \
  -d '{"username": "analyst", "password": "<DEMO_ANALYST_PASSWORD>"}' \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')
curl -s localhost:8080/cases/case_1042 -H "Authorization: Bearer $TOKEN"
curl -s localhost:8080/reviews -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"case_id": "case_1042"}'
```
