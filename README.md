# affirm-agent

## Repository layout

```text
affirm-agent/
├── README.md                     # Overview, how to run, folder guide
├── docker-compose.yml            # Runs all services together
├── .env.example                  # Lists every environment variable, no real values
├── .gitignore                    # .env, node_modules, pycache
├── docs/
│   ├── proposal/                 # Project proposal
│   └── workflow-handoffs.md      # Workflow steps, owners and handoffs
├── contracts/                    # Shared data formats (Pydantic); each producer maintains its own
│   ├── kyb_contracts/
│   │   ├── case_input.py         # CaseInput
│   │   ├── world.py              # World (a golden case's world.json); what case-service endpoints 1–6 return
│   │   ├── case_secrets.py       # CaseSecrets (a golden case's secrets.json): the full TIN and SSNs
│   │   ├── worker_io.py          # WorkerInput (aliases WorkerTask, FollowUp), WorkerResult
│   │   ├── memo.py               # Memo, CheckResult, evidence_index, CheckedMemo; Recommendation
│   │   └── decision.py           # DecisionInput, DecisionRecord
│   └── examples/                 # One valid JSON example per format
├── services/
│   ├── gateway/                  # FastAPI; verifies the JWT and routes by path
│   ├── auth/                     # Login, JWT, roles (auth_db)
│   ├── case/                     # Cases, decisions, audit log (case_db); seed script
│   └── agent/                    # LangGraph workflow and its API; see below
├── frontend/                     # React + React Router; nginx serves the pages and forwards /api to the gateway
├── db/init.sql                   # Creates the three databases and their accounts
├── data/golden/                  # Golden cases: case.json, world.json, secrets.json per case (see its README)
│   ├── ec1/                      # EC-1 cases, one folder per case
│   ├── ec2/                      # EC-2 cases
│   ├── ec4/                      # EC-4 cases
│   └── combo/                    # Cases that combine edge cases
├── eval/                         # Evaluation runner, single-agent baseline, results
└── scripts/validate_golden.py    # Checks that each golden case’s files agree with each other
```

### `services/agent/app/`

```text
services/agent/app/
├── main.py              # FastAPI entry point
├── api/                 # POST /reviews, GET /reviews/{id}, POST /reviews/{id}/decision
├── graph/               # State, orchestration, lead nodes, reconcile, checks
├── workers/             # Records worker, evidence worker
├── tools/               # sos_search, dba_lookup, tin_match, etc., plus the world.json loader
├── clients/             # HTTP client for calling the case service
└── observability/       # LangFuse
```

## Communication schema

The formats live in `contracts/kyb_contracts/`, and the code there is the source of truth. Don't copy the models here; read them there.

### Agent and backend: `CaseInput` ([case_input.py](contracts/kyb_contracts/case_input.py))

The case the existing KYB system hands over, served by the case service at `GET /cases/{case_id}`. Everything arrives already parsed. Field-by-field guide: [case_input_guide.md](contracts/case_input_guide.md).

| Part | Fields |
|---|---|
| Top level | `case_id`, `status`, `flag_reasons` |
| `application` | `legal_name`, `dba`, `address`, `tin_type` (`EIN`/`SSN`), `tin_last4`, `state`, `state_entity_id`, `entity_type`, `formation_date`, `owners` (`ref` such as `owner:P1`, `name`, `role`) |
| `checks` | The KYB vendor's labels: `sos_name` (with `matched_name`), `dba`, `tin`, `commercial_db` |
| `documents` (D) | `id`, `type`, `extracted` (keys per type in `DOCUMENT_FIELDS`), `confidence` |
| `bank_verification` (B) | `id`, `account_holder`, `status`, `verified_at` |
| `web_snapshot` (W) | `id`, `url`, `captured_at`, `extracted` (keys in `WEB_FIELDS`) |
| `owner_kyc` (K) | `id`, `owner_ref`, `result` (`pass`/`fail`/`pending`) |

No full SSN or EIN may appear anywhere in a case; raw OCR and page text stay in case_db.

### Case service and Records worker tools: `World` ([world.py](contracts/kyb_contracts/world.py))

What case-service endpoints 1–6 return ([case-service-endpoints.md](docs/case-service-endpoints.md)). The records come from each golden case's `world.json`, which holds them in these shapes.

| Model | Endpoints | Fields |
|---|---|---|
| `RegistryEntity` | 1, 2 | `state`, `state_entity_id`, `legal_name`, `former_names`, `entity_type`, `entity_status`, `formation_date`, `registered_address`, `officers` |
| `Filing` | 3 | `event_id`, `event_type`, `effective_date`, `old_value`, `new_value` |
| `DbaFiling` | 4 | `filing_id`, `state`, `county`, `dba`, `registrant`, `registrant_type`, `registrant_state_entity_id`, `filed`, `expires`, `status` |
| `CommercialProfile` | 5 | `profile_id`, `business_name`, `address`, `first_seen`, `last_updated` |
| `TinRecord` | 6 | `tin_type`, `tin_last4`, `irs_status`, `irs_name` |

The full numbers live only in each golden case's `secrets.json` (`CaseSecrets`, [case_secrets.py](contracts/kyb_contracts/case_secrets.py)); the case service keeps them in private tables and never returns them. How to write a golden case: [data/golden/README.md](data/golden/README.md).

### Lead and sub-agents: `WorkerInput` / `WorkerResult` ([worker_io.py](contracts/kyb_contracts/worker_io.py))

| Format | Fields |
|---|---|
| `WorkerInput` (aliases `WorkerTask`, `FollowUp`) | `task_id`, `worker` (`records`/`evidence`), `question`, `focus_fields`, `hypotheses`, `context`, `prior_findings`, `follow_up_of`, `max_tool_calls`, `record_id_start` |
| `CaseContext` | Mirrors `CaseInput`: `case_id`, `flag_reasons`, `application` (the same `Application` model), `checks`, and evidence keys only: `documents` (id, type), `bank_verification` (id), `web_snapshot` (id, url, captured_at), `owner_kyc` (id, owner_ref). Workers fetch the stored records by these IDs |
| `WorkerResult` | `task_id`, `case_id`, `worker`, `status`, `findings`, `sources`, `hypothesis_signals`, `not_found`, `unresolved`, `answer`, `tool_calls_used`, `error` |
| `Source` | `source_id` is the evidence ID: R (records the Records worker looks up) or D, B, W, K from the case |

Trust boundary, enforced by the contract: the Records worker cites only R and K sources; the Evidence worker cites only D, B and W. A worker never recommends approve or deny.

### Agent service and review app: `CheckedMemo` ([memo.py](contracts/kyb_contracts/memo.py))

The lead writes the memo (step 8); Checks (code, step 9) adds the evidence index and its result. Once a review is awaiting the analyst, `GET /reviews/{review_id}` returns the three together.

| Format | Fields |
|---|---|
| `Memo` | `case_id`, `flags` (each with `flag`, `status` resolved/unresolved, `root_cause`, `evidence` IDs), `auto_resolved`, `out_of_scope`, `ruled_out` (`cause`, `evidence`), `recommendation`, `document_request` (`type`, `reason`), `notes_for_analyst` |
| `evidence_index` | For each ID the memo cites: `source`, `type`, `excerpt`, `as_of` (`EvidenceEntry`) |
| `CheckResult` | `passed`, `failures` (`rule`, `flag`, `evidence`, `detail`) |

`recommendation` is one of `recommend_clear`, `request_documents`, `escalate` and `insufficient_evidence`. The lead writes evidence IDs only; excerpts come from code. Checks, not the model, applies the hard rules, so a memo that breaks one still validates and goes back to the lead once.

### Review app, agent API and case service: `DecisionInput` / `DecisionRecord` ([decision.py](contracts/kyb_contracts/decision.py))

| Format | Fields |
|---|---|
| `DecisionInput` | `decision` (`approve`/`request_docs`/`escalate`/`deny`), `note`: the body of `POST /reviews/{review_id}/decision` |
| `DecisionRecord` | `review_id`, `case_id`, `decision`, `note`, `analyst_id` (from the JWT), `agent_recommendation`, `overridden`, `decided_at`: what the case service saves in step 11 |

`AGREES_WITH` lists the decisions that agree with each recommendation; `is_override` applies it, and evaluation uses `overridden` for the override rate.

Examples: `contracts/examples/` (`case_input/*.json`, `world/`, `case_secrets/`, `worker_input_*.json`, `worker_result_records.json`, `checked_memo.json`).
