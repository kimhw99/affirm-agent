# Databases

One PostgreSQL server holds three databases, one per service. This doc covers what each holds, how they're created, and the tables in `case_db`.

- Databases and accounts: [db/init.sql](../db/init.sql)
- `case_db` tables: [services/case/schema.sql](../services/case/schema.sql)
- Loading the golden cases: [services/case/seed.py](../services/case/seed.py)
- What the case service serves from them: [case-service-endpoints.md](case-service-endpoints.md)

## Three databases

| Database | Account | Service | Holds | Tables created by |
|---|---|---|---|---|
| `auth_db` | `auth_svc` | Auth service (XL) | Users, password hashes and roles, for login and the JWT | The auth service (to add, BEAC-14) |
| `case_db` | `case_svc` | Case service (XL) | The cases, the mocked official and vendor records, and the decisions, case statuses and audit log from step 11 | The case service, from `schema.sql` |
| `agent_db` | `agent_svc` | Agent service (H) | The graph's checkpoints, so a review paused at step 10 survives a restart. The review records (review_id, status) probably go here too; to agree in BEAC-13 | LangGraph's `PostgresSaver.setup()` |

Each database admits only its own account. The agent service has no account on `case_db`: it reads cases and records only through the case service's endpoints, which never return raw OCR or page text. No database holds a full TIN or SSN (see Rules below).

## Setting up

`docker-compose.yml` runs PostgreSQL 16 as the `db` service. The first time it starts with an empty data volume, it runs `db/init.sql`, which creates the databases and accounts. Both read their settings from `.env` (see `.env.example`):

| Variable | Used for |
|---|---|
| `POSTGRES_PASSWORD` | The postgres image's superuser |
| `AUTH_DB_PASSWORD`, `CASE_DB_PASSWORD`, `AGENT_DB_PASSWORD` | Each service's account; `init.sql` creates it with this password, and the service connects with it |
| `POSTGRES_HOST_PORT` | The port on this machine where the database is published, 55432 by default. Only this machine can connect |

If a password is missing, `docker compose` stops with an error that names it.

To start the database and the case service, and load the golden cases, from the repo root:

```bash
cp .env.example .env                            # then replace each change-me and the XXX token
docker compose up -d                            # the database's first start runs db/init.sql
docker compose run --rm case python seed.py     # loads data/golden into case_db
```

The case service is then at `http://localhost:8001` (`CASE_HOST_PORT`); every endpoint except `/health` needs `Authorization: Bearer <CASE_SERVICE_TOKEN>`. To run `seed.py` on this machine instead of in the container: `pip install -r services/case/requirements.txt`, then `python services/case/seed.py`.

Both `seed.py` and the case service run `schema.sql` when they start. Every statement uses `IF NOT EXISTS`, so running it again changes nothing. Running `seed.py` again replaces each case's rows; `--reset` empties `case_db` first. To change a table, edit `schema.sql`, delete the database volume with `docker compose down -v` (all three databases), then start the database and run `seed.py` again. The data always comes from `data/golden/`, so nothing is lost.

## case_db

| Schema | Holds | Loaded from |
|---|---|---|
| `public` | The cases, served as `CaseInput` (`GET /cases/{case_id}`, endpoints 7–10), and the decisions and audit log | `case.json`; step 11 writes the rest |
| `mock` | The mocked registry, DBA, commercial and IRS records (endpoints 1–6) | `world.json` |

Every table hangs off `cases`. Each indented line references the one above it, and primary keys are in parentheses:

```text
cases (case_id)                                    <- case.json
├── case_owners (case_id, owner_ref)
│   └── owner_kyc (case_id, kyc_id)                endpoint 7
├── documents (case_id, doc_id)                    endpoint 8
├── bank_verifications (case_id, bank_id)          endpoint 9
├── web_snapshots (case_id, web_id)                endpoint 10
├── decisions (review_id)                          step 11
├── audit_log (id)                                 step 11
├── mock.tin_records (case_id)                     endpoint 6 (world.json's tin_record)
└── via source_case: the rest of world.json        <- world.json
    ├── mock.registry_entities (state, state_entity_id)   endpoints 1, 2
    │   └── mock.registry_filings (event_id)              endpoint 3
    ├── mock.dba_filings (filing_id)                      endpoint 4
    └── mock.commercial_profiles (profile_id)             endpoint 5
```

Endpoint 6 returns the case's row in `mock.tin_records` as a `TinRecord`. The other `mock` tables are searched across all cases, like one real registry; this one is read only by `case_id`, one row per case. A TIN the IRS doesn't know is a row with `irs_status` = `not_found`. A case with no TIN has no row, and endpoint 6 returns 404.

### Tables and contracts

| Tables | Contract |
|---|---|
| `cases`, `case_owners`, `documents`, `bank_verifications`, `web_snapshots`, `owner_kyc` | `CaseInput` (`case_input.py`) |
| `mock.*` | `World` (`world.py`) |
| `decisions` | `DecisionRecord` (`decision.py`) |
| `audit_log` | None; only the case service uses it |

The tables aren't a copy of the contracts. A contract describes what crosses a service boundary; a table is how the case service stores it. So some columns have no contract field (`source_case`, `raw_text`, `created_at`), and some parts of a contract are stored whole as JSONB (`checks`, `extracted`, `officers`). `worker_io.py` and `memo.py` have no tables here: their data stays in the agent service.

### Rules

- **Values are checked by the contracts, not the database.** The seed script loads only golden cases that pass `scripts/validate_golden.py`, and step 11 saves a validated `DecisionRecord`. The database adds keys, references and `NOT NULL`, plus a check on the case status, the one value the case service changes itself. So most contract changes need no table change: new keys in `extracted`, a new document type, a new flag. A new application field, evidence type or kind of world record does.
- **Evidence IDs are unique only within a case**, so the keys of those tables start with `case_id`.
- **Deleting a case deletes all its rows**, including its rows in `mock`, so one case can be reloaded on its own. A case that has decisions or audit log entries can't be deleted, so its history can't disappear by accident. A full reload empties everything with `TRUNCATE cases CASCADE`.
- **No full TIN or SSN in any table.** The tables hold what `CaseInput` and `world.json` hold: a TIN's type and last 4 digits, and owners as `owner:P1`. The full numbers stay with the existing KYB system, and no golden case holds one.
- **Never returned by any endpoint**: `documents.raw_text` and `web_snapshots.page_text` (raw merchant text; nothing fills them yet), and every `source_case`.
- **All golden cases share the `mock` tables**, like one real registry, so their record IDs must be unique across cases. `validate_golden.py` checks this.
