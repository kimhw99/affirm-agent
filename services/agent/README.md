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
.venv/bin/python -m pytest -q              # no network or API key needed
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
| `app/graph/worker_notes.py` | 6–8 | What the workers say about their own results (`not_found`, `unresolved`, status, answer, signals), tied to checks | No |
| `app/graph/memo_writer.py` | 8 | Builds the `Memo`; recommendation and confidence are code | Writes root causes, ruled-out causes, notes |
| `app/graph/guardrails.py` | 9 | Builds `evidence_index` from saved data; enforces the hard rules | No |
| `app/graph/workflow.py` | all | The LangGraph graph, `Deps` (injected workers, LLM, clock, result sink) | – |
| `app/graph/llm.py` | – | `LeadLLM` protocol, `OpenAILeadLLM` (LangChain), `NullLeadLLM` | – |
| `app/graph/worker_adapter.py` | 4–5 | `LangGraphWorker`: the BEAC-7 worker functions as `run(WorkerInput)` workers | – |
| `app/api/` | 10 | `POST /reviews`, `GET /reviews/{id}`, `POST /reviews/{id}/decision` | – |
| `app/clients/case_service.py` | 1, 11 | Fetch a case; send the decision back | – |
| `contracts/kyb_contracts/memo.py` | 8–9 | The agreed memo (BEAC-22): `Memo`, `CheckResult`, `EvidenceEntry`, `CheckedMemo` | – |
| `app/review.py` | 8–10 | What the Lead adds (agent-owned, not in `/contracts`): `LeadReport` (`CheckOutcome`s, confidence, route, follow-ups), `ReviewOutput` | – |

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
findings and doesn't rest on the website alone; otherwise it stays a CONFLICT. An EC-1
name or DBA-owner difference also needs a citation that links the names: a former name,
articles of amendment, or a DBA the entity filed itself (`name_link_facts`). HARD_FAILs
(TIN never issued, entity dissolved or revoked, KYC failed, EIN letter ≠ submitted EIN,
EIN belongs to another entity, DBA registered under another entity number, submitted
entity number belongs to another company) are never sent to the LLM.

The EC-1 checks settle who owns the names from official data, without the LLM:
`EC1.applied_name` compares the legal name typed on the application with the registered
name at the submitted entity number (AGREE when equal after normalization or punctuation;
EXPLAINED by a former name or a DBA the entity filed; HARD_FAIL
`entity_id_belongs_to_other_entity` when none of the entity's officers is an applicant
owner; otherwise a CONFLICT only a document settles). `EC1.dba_owner` is a GAP when the
DBA is filed by an owner personally, and a HARD_FAIL `dba_registered_to_other_entity`
when its live filing names another entity number. Decision
rules, in order: any HARD_FAIL → escalate; an open required check → follow-up; else →
clear. Confidence is the weakest required check (enforced by the `LeadReport` contract).
Trust: when bank or documents and the registry agree, the website is ignored; when the
website is the only support, confidence is capped at low.

**Follow-ups.** `PLAYBOOK` lists, per check, the internal steps (rerun the TIN match
with the letter's or the owner's name; search county DBA records; read the filing
history; read the registry status for a pending formation) and the merchant document (147C, DBA certificate,
stamped articles, a current business license). Each follow-up targets one check and one worker; only
that worker runs, and only that check is re-reconciled. The loop stops on: nothing open,
a HARD_FAIL, `MAX_INTERNAL_ROUNDS` (3), a round with no new findings, or no internal step
left. What's still open becomes one merchant round (`request_documents`), and the
playbook never asks for a document type the case already has, except a renewal: a DBA
check that is a CONFLICT (the filing expired or was withdrawn) asks for a current DBA
certificate again (`MerchantAsk.renew_ask`, `renewal_allowed`). The pending-filing step also
tells the Records worker where else to look: the formation state and entity number on the
articles when they differ from the application's (only a two-letter state and a well-formed
entity number are passed on), and for a general partnership, its DBA and partners' KYC. If a worker failed, the
memo says `insufficient_evidence` instead of asking the merchant.

**What the workers say about their own results.** Verdicts come only from findings, but
the Lead also reads the rest of each `WorkerResult` (`app/graph/worker_notes.py`). A
`not_found` or `unresolved` item is tied to a check when it came from a follow-up for
that check or names one of the check's focus fields. An `unresolved` item sends the
check's next follow-up to that worker first, and every follow-up question carries what
the worker left open and what it already searched for in vain. Each follow-up records
the playbook `step` it ran, so steps are skipped by name, not by count. In the memo, an
open check says why it is open in the workers' words, `failed` tasks are worker
failures, and `partial` tasks (a lookup or the summary failed, or the budget ran out)
are listed as warnings. `answer` and `hypothesis_signals` go to the memo-prose LLM; they
come from the same summary call as `not_found` and `unresolved`, so they cost nothing
extra.

**Name matching has one owner: the Lead.** Whether a records name and an evidence name
are the same business is decided in `reconcile.py`, the only place that sees both
sides. The tools still compare names with the application, but only as `name_checks`
hints in the tool reply the worker's model reads (field names and labels, never the
merchant's text), to choose its next lookup. They write no `name_comparison`,
`account_legal_name` or `account_dba` findings.

**Only checks the data can close.** Every fact a check reads is one some tool reports.
The case service and `world.json` have no pending-filing record, domain age or
tampering signal, so no check depends on them. A filed but unapproved formation is the
registry's `entity_status: pending`: the entity isn't active yet, so `EC4.registry_presence`
stays a GAP, even for a new business, and the merchant is asked for good standing. An
entity registered only in another state than the one it applied in (formed in Delaware) is
a GAP too, settled by a good standing or authority certificate from the applied state. A
general partnership needn't register: its absence is EXPLAINED when a live DBA filing names
every partner and every partner passed KYC. With no IRS letter in the case, an EIN whose
`EC2.tin_match` is settled by an IRS Match also settles `EC2.name_control` and
`EC2.ein_letter_matches`, which would otherwise wait for a 147C. `EC4.operating` needs a verified bank
account plus an active license, a website naming the business, or a DBA certificate.
`document_tampered` stays in `HardFailReason` for the API's sake, but nothing sets it.

**Memo and checks.** Code fills the structured parts; the LLM writes prose, which is
scrubbed for full TINs. Step 9 builds `evidence_index` from `CaseInput` and saved
sources, then checks memo.py's rules (CITED_ID_MUST_EXIST, CLEAR_NEEDS_TWO_SOURCES,
UNRELATED_EIN_MUST_ESCALATE, NOT_ISSUED_TIN_CANT_CLEAR, NO_FULL_TAX_ID) and the Lead's
own (HARD_FAIL_NOT_ESCALATED, REQUIRED_CHECK_MISSING, DOCUMENT_ALREADY_IN_CASE,
WEBSITE_OVERRULES_RECORDS). A failing memo is sent back once; if it still fails, it goes to
the analyst with the failures shown.

## The memo and the Lead's report

The memo is `memo.Memo`, as agreed in BEAC-22: one outcome per open flag (status, root
cause, evidence IDs), the causes ruled out, one recommendation, the documents to
request, notes. What the Lead produces beyond that lives in `app/review.py` (`LeadReport`), so
`/contracts` stays exactly as on main:

| In the Lead's report | Why it isn't in the memo |
|---|---|
| `checks`: one `CheckOutcome` per planned check (status, values, evidence, explanation, hard-fail reason) | The memo works per flag; the brief's reconcile works per check |
| `confidence` (the weakest required check) | Not a memo field |
| `route`, `route_reason` (`reviewed`, `needs_info`, `human_queue`) | Intake's decision, not the Lead's conclusion |
| `provisional`, `recheck_after`, `recheck_targets` | EC-4 provisional clear |
| `follow_ups` (internal rounds and the one merchant round) | History, not a conclusion |
| `flag_checks`, `document_checks` | `FlagOutcome` and `DocumentRequest` have no `check_id` |

`GET /reviews/{id}` returns a `ReviewOutput`: `checked_memo` (memo.py's `CheckedMemo`,
unchanged: memo + `evidence_index` + `check_result`), `lead`, `lead_check_failures`
(the Lead's rules beyond `memo.CheckRule`: HARD_FAIL_NOT_ESCALATED,
REQUIRED_CHECK_MISSING, DOCUMENT_ALREADY_IN_CASE, WEBSITE_OVERRULES_RECORDS),
`check_attempts`, and `report_evidence_index` (excerpts for IDs only the report cites).
A memo must name a flag, so a payload with no flags, or one that doesn't parse, has
`checked_memo: null` and the reason in `lead.route_reason`.

Step 9 reports memo.py's rules under their names there: CITED_ID_MUST_EXIST,
CLEAR_NEEDS_TWO_SOURCES, UNRELATED_EIN_MUST_ESCALATE, NOT_ISSUED_TIN_CANT_CLEAR,
NO_FULL_TAX_ID. `bank_letter` isn't a `DocumentType`, so a bank-account conflict asks
for the DBA certificate or articles of amendment.

## Handoffs

**Workers (BEAC-7).** The graph calls `worker.run(WorkerInput) -> WorkerResult`.
`app/main.py` wraps `app.workers.run_records_worker` / `run_evidence_worker` with
`LangGraphWorker`, which passes the workers' case-service client (`HttpCaseClient`),
the Lead's callbacks (worker steps nest under the review's trace) and the Lead's clock.
Without `CASE_SERVICE_URL` (or without the workers), a placeholder fails each task with
the reason, and the memo says insufficient_evidence instead of guessing.

How the Lead reads the workers' findings is listed at the top of `app/graph/facts.py`.
Two points matter:
- `search_registry` returns look-alike entities. Registry facts are split by entity:
  the applicant's entity (its `state_entity_id`, else the closest name) feeds the
  registry checks; every other entity is a separate company. A dissolved look-alike
  never fails the applicant, and a TIN that matches another entity's name is a
  hard fail (`ein_belongs_to_other_entity`).
- The name a TIN match was run with is read from the R source's locator
  (`name=<name>`), since the IRS result itself never carries it.

Follow-up tasks put the check ID first in `question` (`[EC2.tin_match] Run the TIN
match again with …`) and pass prior findings whose `field` is in the step's focus list
(the workers' field names). Parallel Records follow-ups get separate `record_id_start`
ranges (20 apart).

`tests/fakes.py` writes findings the way the BEAC-7 tools do, and
`tests/test_real_workers.py` runs the real workers and tools over the golden cases in
`tests/fixtures/` with a deterministic chat model that follows the workers' prompts
(skipped where the workers aren't installed).

| Golden case | Story | Lead's recommendation |
|---|---|---|
| case_1042 | Renamed LLC; the IRS and the DBA filing have the former name | recommend_clear |
| case_4101 | Suffix and '&' only | recommend_clear (intake resolves it) |
| case_4102 | DBA filed at the county by the LLC | recommend_clear |
| case_4103 | A dissolved look-alike entity in the registry | recommend_clear |
| case_4201 | The DBA was entered as the legal name | recommend_clear (TIN EXPLAINED by registry + DBA filing) |
| case_4202 | The TIN matches none of the merchant's names; 147C read with low confidence | insufficient_evidence |
| case_4401 | Brand-new LLC, EIN not issued yet | request_documents (irs_147c) |
| case_4402 | Sole proprietor; hidden instructions on the website | recommend_clear; W1 flagged in the notes |

**To BEAC-5 (review app, integration).** `GET /reviews/{id}` returns `status` plus a
`ReviewOutput` once `awaiting_decision`. `POST /reviews` takes only `{case_id}` and reads
the case from the case service. It and the decision endpoint read the user from
`X-User-Id` / `X-User-Role` (set by the gateway after it verifies the JWT) and check the
role against the case's current status: either role for a `pending_review` case, only
`senior_analyst` for an `escalated` one, neither for any other (`docs/auth.md`, Roles).
The result goes back via
`POST {CASE_SERVICE_URL}/cases/{case_id}/decisions` with
`{decision_record, case_status}`; that path is a proposal to confirm.

## Environment

Shared with the workers (in the repo's `.env.example`): `OPENAI_API_KEY`,
`OPENAI_MODEL`, `CASE_SERVICE_URL`, `CASE_SERVICE_TOKEN`. The Lead also reads these
optional ones (defaults in brackets):

| Variable | What it does |
|---|---|
| `LEAD_MODEL` [`OPENAI_MODEL`, then gpt-4o-mini] | The Lead's model, if it should differ from the workers' |
| `LEAD_LLM` [on] | `off` forces the deterministic Lead even with a key (tests, evaluation baseline) |
| `SEND_RESULTS` [1] | `0` doesn't send the final decision back to the case service (local demos) |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` [unset] | LangFuse tracing |

## Open items

- `MAX_INTERNAL_ROUNDS` is 3 per the brief; `docs/workflow-handoffs.md` still says 2.
- `/contracts` and `.env.example` are left exactly as on main so the branch merges
  without conflicts. `LeadReport` / `ReviewOutput` live in `app/review.py` (example:
  `examples/review_output_case_1042.json`). If the review app wants a shared Python
  model for them, promote that module to `kyb_contracts` with the team's agreement;
  after the merge, add the variables above to `.env.example`.
- `bank_letter` as a `DocumentType` (case_input.py), so a bank-account conflict can ask for one.
- Postgres checkpointer and a persistent idempotency table (both in memory today).
