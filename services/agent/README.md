# Agent service: orchestrator and Lead agent (BEAC-3)

The LangGraph workflow that takes a `CaseInput`, routes it, plans the checks, runs the
Records and Evidence workers in parallel, reconciles the two sides, runs targeted
follow-ups, writes the memo, checks it, pauses for the analyst, and sends the decision
back to the backend.

**The main rule:** the LLM is used only where judgment is needed. Routing, hard fails,
statuses, the recommendation, confidence and the follow-up stopping rules are plain,
tested code. The LLM adds optional checks, decides whether a difference is EXPLAINED
(with citations), rewords follow-up asks and writes the memo's prose. With no API key,
every LLM call falls back to a deterministic path, so the graph and tests run offline.

## Run it

```bash
cd services/agent
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q              # 194 tests, no network or API key needed
.venv/bin/uvicorn app.main:app --reload    # the /reviews API
```

The shared contracts are on the path through `pytest.ini` (`pythonpath = . ../../contracts`).
For uvicorn, install them: `pip install -e ../../contracts`.

## Where things are

| File | Step | What it does | LLM? |
|---|---|---|---|
| `app/graph/orchestrator.py` | 2 | Validates the payload, auto-resolves formatting-only flags, filters out-of-scope flags, checks the inputs, and looks up which workers and lookups run (`ROUTING_TABLE`). Idempotency key | No |
| `app/graph/plan.py` | 3 | Fixed check templates per edge case (`TEMPLATES`), `expected_absence`, `WorkerInput` construction | Adds optional `ADD.` checks, hypotheses, questions |
| `app/graph/facts.py` | – | Index over worker findings by logical field and source kind | No |
| `app/graph/reconcile.py` | 6 | One comparator per check → AGREE / EXPLAINED / GAP / CONFLICT / HARD_FAIL; hard-fail sweep; decision rules | Judges EXPLAINED only, with citations |
| `app/graph/followup.py` | 7 | Follow-up playbook (internal steps, then a merchant document), stopping rules | Rewords asks |
| `app/graph/memo_writer.py` | 8 | Builds the `Memo`; recommendation and confidence are code | Writes root causes, ruled-out causes, notes |
| `app/graph/guardrails.py` | 9 | Builds `evidence_index` from saved data; enforces the hard rules | No |
| `app/graph/workflow.py` | all | The LangGraph graph, `Deps` (injected workers, LLM, clock, result sink) | – |
| `app/graph/llm.py` | – | `LeadLLM` protocol, `OpenAILeadLLM` (LangChain), `NullLeadLLM` | – |
| `app/api/` | 10 | `POST /reviews`, `GET /reviews/{id}`, `POST /reviews/{id}/decision` | – |
| `app/clients/case_service.py` | 1, 11 | Fetch a case; send the decision back | – |
| `contracts/kyb_contracts/memo.py` | 8–9 | `Memo`, `CheckOutcome`, `CheckResult`, `ReviewOutput`, `evidence_index` | – |

## The graph

```
intake ──(reviewed)──> plan ──Send×2──> run_worker ──> reconcile ──> follow_up ──┐
   │                                        ^                                     │
   │                                        └────── Send (one per open check) ────┘
   └──(human_queue / needs_info / nothing open)──────────────> write_memo <───────┘
                                                                  │   ^
                                                               checks ┘  (sent back once)
                                                                  │
                                                     human_review (interrupt)
                                                                  │
                                                     finalize ──> backend API
```

`thread_id` = `review_id`. The default checkpointer is in memory; switch to
`langgraph-checkpoint-postgres` for restarts to survive (architecture.md, step 10).

## How each rule from the brief is implemented

**Orchestrator (deterministic).** `ROUTING_TABLE` maps each flag to its Records and
Evidence lookups; several flags merge and de-duplicate their lists. Workers run in
parallel via `Send`, and each only sees its own side (`prior_findings` come from the
same worker). Before any agent runs: EC-2 with neither a TIN nor an EIN letter →
`needs_info` (request_documents). No flags, unknown flags, only out-of-scope flags, or a
malformed payload → `human_queue` with a reason. The idempotency key is
`case_id:<sha256 of the canonical input>`, so a retry doesn't start a second run, but a
case that comes back after `docs_requested` with new evidence does.

**Plan.** Each check carries its records side, evidence side and pass rule. The LLM's
`PlanExtras` can only add `ADD.` checks, always `required=False`; `build_plan` asserts
every template check is still there and still required. `EC4.registry_presence` sets
`expected_absence` up front for sole proprietors and businesses formed within
`REGISTRY_LAG_DAYS` (90).

**Reconcile.** Code compares first (exact EIN last 4, normalized names via
`app/shared/name_norm.py`, dates). Only a CONFLICT that's flagged explainable goes to the
explainer, and `apply_explanation` accepts an explanation only when it cites real
findings and doesn't rest on the website alone; otherwise it stays a CONFLICT. HARD_FAILs
(TIN never issued, entity dissolved or revoked, KYC failed, EIN letter ≠ submitted EIN,
EIN belongs to another entity, tampered document) are never sent to the LLM. Decision
rules, in order: any HARD_FAIL → escalate; an open required check → follow-up; else →
clear. Confidence is the weakest required check (enforced by the `Memo` contract).
Trust: when bank or documents and the registry agree, the website is ignored; when the
website is the only support, confidence is capped at low.

**Follow-ups.** `PLAYBOOK` lists, per check, the internal steps (rerun the TIN match
with the letter's or the owner's name; search county DBA records; read the filing
history; check pending filings) and the merchant document (147C, DBA certificate,
stamped articles, bank letter). Each follow-up targets one check and one worker; only
that worker runs, and only that check is re-reconciled. The loop stops on: nothing open,
a HARD_FAIL, `MAX_INTERNAL_ROUNDS` (3), a round with no new findings, or no internal step
left. What's still open becomes one merchant round (`request_documents`), and the
playbook never asks for a document type the case already has. If a worker failed, the
memo says `insufficient_evidence` instead of asking the merchant.

**Memo and checks.** Code fills the structured parts; the LLM writes prose, which is
scrubbed for full TINs. Step 9 builds `evidence_index` from `CaseInput` and saved
sources, then checks CITATION_NOT_FOUND, CLEAR_NEEDS_TWO_SOURCES,
EIN_OTHER_ENTITY_NOT_ESCALATED, NOT_ISSUED_TIN_CLEARED, HARD_FAIL_NOT_ESCALATED,
REQUIRED_CHECK_MISSING, DOCUMENT_ALREADY_IN_CASE, FULL_TIN_IN_MEMO and
WEBSITE_OVERRULES_RECORDS. A failing memo is sent back once; if it still fails, it goes to
the analyst with the failures shown.

## Handoffs

**To BEAC-4 (workers).** A worker is any object with `run(WorkerInput) -> WorkerResult`.
`app/main.py` loads `app.workers.records.RecordsWorker` and
`app.workers.evidence.EvidenceWorker`; until they exist, a placeholder returns `failed`.
Reconcile reads findings by `field` name; the accepted names are listed at the top of
`app/graph/facts.py` (for example `legal_name`, `entity_status`, `dba_owner`,
`former_name` / `name_change`, `tin_match` as a result, an IRS code or
`{"result", "name_tested"}`, `irs_name_control`, `kyc_result`, `registry_found`,
`separate_entity`; on the evidence side `name`, `ein_last4`, `date`, `account_holder`,
`bank_status`, `registrant`, `tin_type`, `document_tampered`, `suspicious_content`).
Follow-up tasks put the check ID first in `question` (`[EC2.tin_match] Run the TIN match
again with …`). Parallel Records follow-ups get separate `record_id_start` ranges (20
apart). `tests/fakes.py` shows the shape the Lead expects.

**To BEAC-5 (review app, integration).** `GET /reviews/{id}` returns `status` plus a
`ReviewOutput` once `awaiting_decision`. The decision endpoint reads the user from
`X-User-Id` / `X-User-Role` (set by the gateway after it verifies the JWT; to confirm);
`analyst` and `senior_analyst` may decide. The result goes back via
`POST {CASE_SERVICE_URL}/cases/{case_id}/decisions` with
`{decision_record, case_status}`; that path is a proposal to confirm.

## Open items

- `MAX_INTERNAL_ROUNDS` is 3 per the brief; `docs/workflow-handoffs.md` still says 2.
- `memo.py` was empty in the repo; it was rebuilt to satisfy `tests/test_memo.py` and the
  example `review_output_case_1042.json` (all pass).
- Postgres checkpointer and a persistent idempotency table (both in memory today).
