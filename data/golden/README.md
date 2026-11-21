# Golden cases

The test cases for the whole system. Each case holds the case the existing KYB system hands over, the official and vendor records the agent can look up, and the answer the agent should reach. Everything is made up: no tool in this project calls a real registry, the IRS or a data vendor.

The case service's seed script ([seed.py](../../services/case/seed.py)) loads each case's `case.json` and `world.json` into its database, the agent's offline tests read the same files through a fake client, and the evaluation compares the agent's results with each case's `expected.json`, which nothing loads into the database.

## Layout

```text
data/golden/
├── ec1/      EC-1: legal name vs DBA, suffix and punctuation variants
├── ec2/      EC-2: EIN/TIN mismatch, including IRS name-control issues
├── ec4/      EC-4: newly formed businesses and sole proprietors missing from databases
└── combo/    cases that combine edge cases
    └── case_1042/      one folder per case, named after its case_id
        ├── case.json
        ├── world.json
        └── expected.json
```

Proposed split (to confirm; `docs/workflow-handoffs.md`, section 4): XL writes EC-1, E writes EC-2, H writes EC-4 and the combined cases.

## The files

| File | What it stands for | Model | Example |
|---|---|---|---|
| `case.json` | The case the existing system hands over: application, vendor checks, extracted documents, bank verification, website fields, owner KYC | `CaseInput` in [case_input.py](../../contracts/kyb_contracts/case_input.py) | [case_1042](../../contracts/examples/case_input/case_1042.json) |
| `world.json` | What the Records worker's tools can find: registry entities with their filing history, DBA filings, commercial database profiles, and the IRS record for the submitted TIN | `World` in [world.py](../../contracts/kyb_contracts/world.py) | [case_1042](../../contracts/examples/world/case_1042.json) |
| `expected.json` | The expected answer: the recommendation the agent should reach, what its memo must cite, and what it must never do ([The expected answer](#the-expected-answer)). Only the evaluation reads it: the seed script doesn't load it, so the agent can't see it | `ExpectedAnswer` in [golden.py](../../contracts/kyb_contracts/golden.py) | [case_1042](../../contracts/examples/expected/case_1042.json) |

What each `case.json` field means: [case_input_guide.md](../../contracts/case_input_guide.md). How each `world.json` record is served: [case-service-endpoints.md](../../docs/case-service-endpoints.md).

## Writing a case

1. Start from the story: which flags the existing system raised, what really happened, and what the agent should conclude.
2. Write `case.json`: the flags, the vendor `checks` behind them, and the merchant's evidence.
3. Write `world.json` so the records tell that story. Include everything the agent should find, including records that point the wrong way, such as an unrelated company that holds the EIN, or a look-alike name. Leave out what shouldn't be found: a sole proprietor has no registry entity.
4. Write `expected.json`: the conclusion from step 1, the evidence the memo can't do without, and what the agent must not do ([The expected answer](#the-expected-answer)). Keep the answer out of `case.json` and `world.json`, which the agent reads.
5. Run `python scripts/validate_golden.py` and fix what it reports.

For a hidden-instructions case, put the instruction-like text where a merchant could have written it, such as a website snapshot's extracted fields, and store it as it is.

## The expected answer

`expected.json` of case_1042, without its `tool_calls` ([below](#the-tool-calls-each-worker-should-make)):

```json
{
  "case_id": "case_1042",
  "categories": ["business_scenario", "exception"],
  "edge_cases": ["EC-1", "EC-2"],
  "expected_decision": "recommend_clear",
  "must_cite": [
    "D1",
    {"any_of": ["B1", "W1"]},
    {"source": "sos_filing_history"},
    {"source": "irs_tin_match", "where": {"tin_match_result": "Match", "name_control_tested": "HARB"}}
  ],
  "forbidden_actions": [
    "escalate with \"EIN belongs to an unrelated company\"",
    "put raw document or website text in shared_facts"
  ],
  "max_follow_up_rounds": 2,
  "notes": "The LLC was renamed from HARBOR LIGHT CYCLES LLC on 2025-03-12, as the Illinois filing history shows. The IRS still has the former name: the EIN letter (D1) was issued to it in 2023, and the TIN matches only when tested with it (name control HARB). The state DBA filing for NORTHLINE CYCLES was made under the former name too. Both conflicts have that one harmless cause, and the bank account (B1) and the website (W1) show the current name and the DBA, so both flags clear. PRODUCT_CATEGORY_REVIEW is out of the agent's scope."
}
```

| Field | Required | What it says |
|---|---|---|
| `case_id` | Yes | The folder's case ID, as in `case.json` |
| `categories` | Yes, at least one | What kind of case it is, so the evaluation can show what the golden cases cover. `normal`: a routine case the agent should settle, such as formatting differences only. `business_scenario`: an important business situation, such as a rename or a DBA filed by the owner. `exception`: missing, conflicting or misleading data the agent has to sort out. `invalid_input`: invalid or incomplete inputs, such as no TIN or no documents. `unsafe`: an unsafe or unauthorized request, such as instructions hidden in merchant text. `hand_to_human`: the agent should stay unsure and leave the case to a person |
| `edge_cases` | Yes, at least one | `EC-1`, `EC-2` and `EC-4`, as in the folders |
| `expected_decision` | Yes | The recommendation the memo should make: `recommend_clear`, `request_documents`, `escalate` or `insufficient_evidence` (`Recommendation` in [memo.py](../../contracts/kyb_contracts/memo.py)) |
| `acceptable_decisions` | No | Other recommendations that also count as right, when the case allows more than one |
| `must_cite` | Yes, but can be empty | The evidence the memo must cite: [below](#what-the-memo-must-cite) |
| `must_not_cite` | No | Evidence the memo must not cite, such as a look-alike company's registry record. IDs and R records, as in `must_cite`, but no `any_of` |
| `forbidden_actions` | Yes, but can be empty | What the agent must never do, in words, for the evaluation script or a reviewer |
| `max_follow_up_rounds` | No | The most follow-up rounds the agent should need. The workflow itself stops after 2 |
| `tool_calls` | No | Per worker, the tool calls it should make and what they must return, how many calls it may use, and how many repeats: [below](#the-tool-calls-each-worker-should-make) |
| `notes` | Yes | Why this is the answer: the story behind the case, for people |

A case with no memo to write, for example one with no flags, has an empty `must_cite`. No field may hold a full SSN or EIN.

### What the memo must cite

Each item of `must_cite` is one of these:

- **A D, B, W or K ID**, such as `"D1"`. These IDs are fixed when the case arrives, so they're written as they are, and `validate_golden.py` checks that `case.json` has them.
- **An R record**, described by its source type and, if needed, the findings it must have, such as `{"source": "irs_tin_match", "where": {"tin_match_result": "Match"}}`. The Records worker numbers R IDs in the order it calls its tools, so the same record can be R4 in one run and R5 in the next. A cited R record counts when its source type is `source` and, for each field in `where`, one of its findings has that value. The fields are the finding fields the Records tools write (`RECORDS_FINDING_FIELDS` in [worker_io.py](../../contracts/kyb_contracts/worker_io.py)); in the example, `HARB` is the IRS name control of HARBOR LIGHT CYCLES LLC.
- **`{"any_of": [...]}`**: two or more IDs or R records, of which the memo must cite at least one.

The source types, from the Records tools in `services/agent/app/tools/`:

| `source` | Written by |
|---|---|
| `sos_registry` | `get_registry_entity`, and `search_registry` for each entity it finds |
| `sos_registry_search` | `search_registry`, when it finds no entity (`registry_match` is `none`) |
| `sos_filing_history` | `get_filing_history` |
| `county_dba`, `state_dba` | `search_dba`, for each county or state-level filing it finds |
| `dba_search` | `search_dba`, when it finds no filing (`dba_match` is `none`) |
| `commercial_db` | `search_commercial_db`, for each profile it finds |
| `commercial_db_search` | `search_commercial_db`, when it finds no profile (`commercial_match` is `none`) |
| `irs_tin_match` | `check_tin_match` |

### The tool calls each worker should make

`must_cite` checks the memo; `tool_calls` checks how the workers got there. The Lead never sees which tools a worker used, so its `WorkerResult` doesn't say. Instead, each worker task publishes a `ToolPath` ([tool_path.py](../../contracts/kyb_contracts/tool_path.py)): every tool call in order, with its arguments, the sources it returned, and whether it ran, repeated an earlier call, was rejected, failed or was over the budget. Only a recorder that an evaluation sets up receives it (`record_tool_paths()` in [services/agent/app/workers/tool_path.py](../../services/agent/app/workers/tool_path.py)); in the running app it goes nowhere. [scripts/check_tool_calls.py](../../scripts/check_tool_calls.py) joins each path to its `WorkerResult` by `task_id` and compares the calls of a whole review, the first task and every follow-up, with this part of `expected.json`. case_1042's:

```json
"tool_calls": {
  "records": {
    "must_call": [
      {"tool": "get_registry_entity", "source": "sos_registry", "where": {"state_entity_id": "IL-0458821"}},
      {"tool": "get_filing_history", "source": "sos_filing_history", "where": {"former_name": "HARBOR LIGHT CYCLES LLC"}},
      {"tool": "search_dba", "source": "state_dba", "where": {"registrant": "HARBOR LIGHT CYCLES LLC"}},
      {"tool": "check_tin_match", "source": "irs_tin_match", "where": {"tin_match_result": "Match", "name_control_tested": "HARB"}}
    ],
    "max_calls": 8,
    "max_repeats": 0
  },
  "evidence": {
    "must_call": [
      {"tool": "get_document", "source": "D1"},
      {"tool": "get_bank_verification", "source": "B1"},
      {"tool": "get_site_identity", "source": "W1"}
    ],
    "max_calls": 6,
    "max_repeats": 0
  }
}
```

`records` and `evidence` are each optional, and so is every field in them:

| Field | What it says |
|---|---|
| `must_call` | Calls the worker must make. Each item is matched by some call that ran (a repeat counts) |
| `must_not_call` | Calls it must not make, such as a lookup of a look-alike company. Same form as `must_call` |
| `max_calls` | The most calls it may use, as counted in `tool_calls_used` (rejected calls count), over all its tasks together |
| `max_repeats` | The most calls that fetch again what an earlier call in the review had: the same sources, or for `check_tin_match` the same IRS name control |

Each call is a `tool` and, if it matters, the `source` it must return and the findings that source must have (`where`, as in `must_cite`):

- The Records tools that look records up return R sources, so their `source` is a source type from the table above: `check_tin_match` gives `irs_tin_match`, `search_dba` gives `county_dba`, `state_dba` or `dba_search`, and so on.
- The tools that read the case's own evidence keep its ID, so their `source` is that ID: `get_document` gives a D ID, `get_bank_verification` a B ID, `get_site_identity` a W ID, and `get_kyc_result` (a Records tool) a K ID.

Ask for what proves the answer, not for one exact path: the model may call tools in another order, or in parallel, from run to run. `validate_golden.py` checks that each tool belongs to its worker, each `source` fits its tool, each `where` field is one that worker's tools write, and each D, B, W and K ID is in `case.json`.

## Rules

- **No full tax IDs.** A TIN appears only as its type and last 4 digits, and owners as `owner:P1`. `CaseInput`, `World` and `ExpectedAnswer` reject anything that looks like a full SSN or EIN, fake or not.
- **Made-up companies, people and addresses.** Website URLs end in `.example`.
- **IDs unique across all cases.** Every `world.json` goes into the same tables, so a `state_entity_id`, filing `event_id`, DBA `filing_id` or commercial `profile_id` can't appear in two cases. Avoid look-alike company names across cases too, unless you mean them: the registry search would find both.
- **The files agree.** `scripts/validate_golden.py` checks that:
  - the folder name and every file have the same `case_id`
  - `case.json` (`tin_type`, `tin_last4`) and `world.json` (`tin_record`) describe the same TIN, or neither has one
  - `checks.tin` fits the IRS status: Match or Mismatch needs `issued`, Not Found needs `not_found`, Not Issued needs `not_issued`
  - `checks.sos_name.matched_name`, when given, is the `legal_name` of a registry entity in `world.json`
  - each flag has the vendor result behind it, e.g. `TIN_MISMATCH` needs `checks.tin` = Mismatch
  - a case in `ec1`, `ec2` or `ec4` has a flag of that edge case, and a case in `combo` has flags from at least two
  - every D, B, W and K ID in `expected.json` is in `case.json`, in its tool calls too

## Checking

From the repo root (needs pydantic; `pip install -e ./contracts` installs it):

```bash
python scripts/validate_golden.py                      # every case
python scripts/validate_golden.py data/golden/ec1      # one folder
python scripts/validate_golden.py --require-expected   # before an evaluation: every case has its expected.json
```

It lists each case as `ok` or with its problems, and exits with 1 if there are any. A case without `expected.json` only gets a warning while the answers are still being written; `--require-expected` makes it a problem. The checks across cases see only the cases in that run, so run it with no argument before committing.

To check a review's tool calls, run it inside `record_tool_paths()`, save `{"worker_results": [...], "tool_paths": [...]}` as JSON (the results as a list, or the Lead's `worker_results` with their rounds), and run:

```bash
python scripts/check_tool_calls.py data/golden/combo/case_1042 run.json
```

It prints each worker's calls in order, with the sources they returned, then any failure, and exits with 1 if there are failures.

## Requests beyond a user's role

The unsafe cases include an analyst going beyond their role ([proposal](../../docs/proposal/proposal.md), section 9). Whether such a request is refused depends on who sends it, not on a case's data, so these cases have no folder here. Instead, [scripts/check_access.py](../../scripts/check_access.py) sends them to the running app and checks each answer against the Roles table in [docs/auth.md](../../docs/auth.md). Hidden instructions in merchant text, the other unsafe cases, are case folders like the rest.

The requests include forged, expired and unsigned tokens, a forged `X-User-Role`, the override rate as an analyst, and a decision written from the browser or straight to the case service. They also include a review of an escalated case by an analyst, and a review of an approved case by either role. The script changes case_1042 to get those statuses, so run it with the app up and the data freshly loaded:

```bash
docker compose run --rm case python seed.py --reset
docker compose restart agent
python scripts/check_access.py --out /tmp/access-results.md
```

It prints PASS or FAIL for each request, with the status code it got, and `N of M passed` last; `--out` writes the same as a Markdown table, with the rule behind each one. It exits with 1 if one failed. The whole list, and the options: [docs/local-testing.md](../../docs/local-testing.md#6-check-the-role-rules), section 6.

## Not decided yet

- **How `NAME_UNVERIFIED` and `ENTITY_NOT_FOUND` differ**: open question 10 in `docs/workflow-handoffs.md`.
