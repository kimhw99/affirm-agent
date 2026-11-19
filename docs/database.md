# Databases

One PostgreSQL server holds three databases, one per service. This doc covers what each holds, how they're created, and the tables in `case_db`.

- Databases and accounts: [db/init.sql](../db/init.sql)
- `case_db` tables: [services/case/schema.sql](../services/case/schema.sql)
- What the case service serves from them: [case-service-endpoints.md](case-service-endpoints.md)

## Three databases

| Database | Account | Service | Holds | Tables created by |
|---|---|---|---|---|
| `auth_db` | `auth_svc` | Auth service (XL) | Users, password hashes and roles, for login and the JWT | The auth service (to add, BEAC-14) |
| `case_db` | `case_svc` | Case service (XL) | The cases, the mocked official and vendor records, the full TINs and SSNs, and the decisions, case statuses and audit log from step 11 | The case service, from `schema.sql` |
| `agent_db` | `agent_svc` | Agent service (H) | The graph's checkpoints, so a review paused at step 10 survives a restart. The review records (review_id, status) probably go here too; to agree in BEAC-13 | LangGraph's `PostgresSaver.setup()` |

Each database admits only its own account. The agent service has no account on `case_db`: it reads cases and records only through the case service's endpoints, which never return a full TIN or SSN. This is what keeps full numbers away from the model.

## Setting up

`db/init.sql` creates the databases and accounts. PostgreSQL runs it once, the first time the database container starts with an empty data folder (mounted in `/docker-entrypoint-initdb.d/`). It reads the passwords from the environment, so set these in `.env` (see `.env.example`):

| Variable | Used for |
|---|---|
| `POSTGRES_PASSWORD` | The postgres image's superuser |
| `AUTH_DB_PASSWORD`, `CASE_DB_PASSWORD`, `AGENT_DB_PASSWORD` | Each service's account; `init.sql` creates it with this password, and the service connects with it |

If one is missing, the container stops with an error that names it.

The case service runs `schema.sql` when it starts. Every statement uses `IF NOT EXISTS`, so running it again changes nothing. To change a table, edit `schema.sql`, drop the database and load the golden cases again. The data always comes from `data/golden/`, so nothing is lost.

## case_db

| Schema | Holds | Loaded from |
|---|---|---|
| `public` | The cases, served as `CaseInput` (`GET /cases/{case_id}`, endpoints 7–10), and the decisions and audit log | `case.json`; step 11 writes the rest |
| `mock` | The mocked registry, DBA and commercial records (endpoints 1–5) | `world.json` |
| `private` | Every full TIN and SSN, and the mocked IRS records, keyed by full TIN like the real IRS (endpoint 6) | `secrets.json`, and `world.json`'s `tin_record` |

Every table hangs off `cases`. Each indented line references the one above it, and primary keys are in parentheses:

```text
cases (case_id)                                    <- case.json
├── case_owners (case_id, owner_ref)
│   ├── owner_kyc (case_id, kyc_id)                endpoint 7
│   └── private.owner_ssns (case_id, owner_ref)    <- secrets.json
├── documents (case_id, doc_id)                    endpoint 8
├── bank_verifications (case_id, bank_id)          endpoint 9
├── web_snapshots (case_id, web_id)                endpoint 10
├── private.case_tins (case_id)                    <- secrets.json
├── decisions (review_id)                          step 11
├── audit_log (id)                                 step 11
└── via source_case: every row from world.json     <- world.json
    ├── mock.registry_entities (state, state_entity_id)   endpoints 1, 2
    │   └── mock.registry_filings (event_id)              endpoint 3
    ├── mock.dba_filings (filing_id)                      endpoint 4
    ├── mock.commercial_profiles (profile_id)             endpoint 5
    └── private.irs_records (tin)                         endpoint 6
```

Endpoint 6 finds the case's full TIN in `private.case_tins` and looks it up in `private.irs_records`. A TIN the IRS doesn't know has no row, and the endpoint reports `not_found`.

### Tables and contracts

| Tables | Contract |
|---|---|
| `cases`, `case_owners`, `documents`, `bank_verifications`, `web_snapshots`, `owner_kyc` | `CaseInput` (`case_input.py`) |
| `mock.*`, `private.irs_records` | `World` (`world.py`) |
| `private.case_tins`, `private.owner_ssns` | `CaseSecrets` (`case_secrets.py`) |
| `decisions` | `DecisionRecord` (`decision.py`) |
| `audit_log` | None; only the case service uses it |

The tables aren't a copy of the contracts. A contract describes what crosses a service boundary; a table is how the case service stores it. So some columns have no contract field (`source_case`, `raw_text`, `created_at`), and some parts of a contract are stored whole as JSONB (`checks`, `extracted`, `officers`). `worker_io.py` and `memo.py` have no tables here: their data stays in the agent service.

### Rules

- **Values are checked by the contracts, not the database.** The seed script loads only golden cases that pass `scripts/validate_golden.py`, and step 11 saves a validated `DecisionRecord`. The database adds keys, references and `NOT NULL`, plus a check on the case status, the one value the case service changes itself. So most contract changes need no table change: new keys in `extracted`, a new document type, a new flag. A new application field, evidence type or kind of world record does.
- **Evidence IDs are unique only within a case**, so the keys of those tables start with `case_id`.
- **Deleting a case deletes all its rows**, including its rows in `mock` and `private`, so one case can be reloaded on its own. A case that has decisions or audit log entries can't be deleted, so its history can't disappear by accident. A full reload empties everything with `TRUNCATE cases CASCADE`.
- **Never returned by any endpoint**: `documents.raw_text` and `web_snapshots.page_text` (raw merchant text; nothing fills them yet), every `source_case`, and the full numbers in `private`.
- **All golden cases share the `mock` tables**, like one real registry, so their record IDs must be unique across cases. `validate_golden.py` checks this.
