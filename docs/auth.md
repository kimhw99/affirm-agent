# Auth

How users log in, what the JWT carries, and how the gateway tells the backend services who is acting.

- Login: [services/auth/](../services/auth/)
- The checks on every request: [services/gateway/](../services/gateway/)
- The formats (roles, login, the JWT's claims): [contracts/kyb_contracts/auth.py](../contracts/kyb_contracts/auth.py)

## Roles

| Role | Who | Can |
|---|---|---|
| `analyst` | Reviews cases | Open cases and reviews, start a review and decide it. The queue shows the cases waiting for review (`pending_review`) |
| `senior_analyst` | Takes escalated cases | The same. The queue shows the escalated cases |

The queue itself comes with the review app (BEAC-15).

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

The gateway is the only service the browser talks to: `http://localhost:8080` (`GATEWAY_HOST_PORT`).

| Path | Goes to | Needs a JWT | Methods |
|---|---|---|---|
| `/auth/...` | Auth service | No: the auth service checks its own credentials | GET, POST |
| `/cases/...` | Case service | Yes | GET |
| `/reviews/...` | Agent service | Yes | GET, POST |

For `/cases` and `/reviews`, the gateway:

1. Verifies the JWT: 401 if it's missing, invalid or expired, and 403 if its role isn't one of the two above.
2. Drops any `X-User-*` header the client sent, and sets `X-User-Id` (the `sub`) and `X-User-Role`.
3. Doesn't pass the JWT on. Requests to the case service carry the service token (`CASE_SERVICE_TOKEN`) instead.

Only reads reach the case service. `POST /cases/{case_id}/decisions` is for the agent service, so a decision always goes through its review (`POST /reviews/{review_id}/decision`). Any other path is 404.

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
