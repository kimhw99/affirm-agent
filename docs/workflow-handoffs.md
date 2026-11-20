# KYB Exception Review: Workflow Steps, Owners and Handoffs

Who builds each step of the workflow, what each step hands to the next, and when. Task status lives in Jira; this doc is the team's agreement on steps, formats and handoffs.

Related docs:

- `docs/proposal/proposal.md`: the scope and why it needs an agent
- `docs/architecture.md`: how the services and the agent fit together
- `contracts/case_input_guide.md`: every `CaseInput` field and the KYB terms behind it

Owner initials, as in Jira:

- **H**: BEAC-3 Orchestration and Lead agent ("brain")
- **E**: BEAC-4 Records and Evidence workers ("hands")
- **XL**: BEAC-5 Review app and integration ("face")

Items marked **(to confirm)** are proposals the team still needs to agree on.

## 1. Overall flow

```
1  Case JSON (existing system)              XL
      │ CaseInput
      ▼
2  Intake (code)                            H (to confirm: could move to XL)
      │ open_flags + normalized (no open flags → straight to step 8)
      ▼
3  Lead: plan (LLM)                         H
      │ WorkerInput × 2
      ▼
4  Records worker  ┐ in parallel            E
5  Evidence worker ┘                        E
      │ WorkerResult × 2 (Records, Evidence)
      ▼
6  Reconcile (code)                         H
      │ ReconcileTable
      ▼
7  Lead: follow up? (LLM)                   H  ── FollowUp (max 2 rounds) ──→ step 4 or 5, then back to step 6
      │ all resolved, or time to stop
      ▼
8  Lead: write memo (LLM)                   H  ←── sent back once if step 9 fails
      │ Memo (evidence IDs only)
      ▼
9  Checks (code)                            H
      │ Memo + evidence_index + CheckResult
      ▼
10 Analyst decision (interrupt)             H (graph node) + XL (API, UI, access control)
      │ DecisionInput
      ▼
11 Decision saved                           XL  → DecisionRecord, audit log
```

Work that spans the whole flow: Auth and API gateway (XL), Agent service API (XL + H), LangFuse (H), Evaluation (everyone), Deployment (XL).

## 2. Data formats at a glance

Rule: whoever produces the data owns its format; whoever uses it reviews the format.

| Format | Main fields | Producer (owns the format) | Consumers (review it) | Defined in |
|---|---|---|---|---|
| `CaseInput` | case_id, status, flag_reasons, application, checks, documents, bank_verification, web_snapshot, owner_kyc | XL | H, E | `contracts/kyb_contracts/case_input.py` |
| `World` (a golden case's `world.json`) | case_id, registry (with filings), dba_filings, commercial, tin_record; the records are what case-service endpoints 1–6 return | XL | E | `contracts/kyb_contracts/world.py` |
| `WorkerInput` (aliases `WorkerTask`, and `FollowUp` when `follow_up_of` is set) | task_id, worker, question, focus_fields, hypotheses, context (`CaseContext`: the application, checks and evidence IDs from `CaseInput`), prior_findings, follow_up_of, max_tool_calls, record_id_start | H | E | `contracts/kyb_contracts/worker_io.py` |
| `WorkerResult` | task_id, case_id, worker, status, findings, sources (each `source_id` is an evidence ID), hypothesis_signals, not_found, unresolved, answer, tool_calls_used, error | E | H | `contracts/kyb_contracts/worker_io.py` |
| `ReconcileTable` | Per field: records_value, evidence_value, status, evidence_ids | H | H | Proposed here |
| `Memo` | flags (status, root_cause, evidence), auto_resolved, out_of_scope, ruled_out, recommendation, document_request, notes_for_analyst | H | XL | Proposed here; `memo.py` (to add) |
| `evidence_index` | For each cited ID: source, type, excerpt, as_of | H (built by code in step 9) | XL | Proposed here; `memo.py` (to add) |
| `CheckResult` | passed, failures | H | XL | Proposed here; `memo.py` (to add) |
| `DecisionInput` / `DecisionRecord` | decision, note, analyst_id, agent_recommendation, overridden, decided_at | XL | H (evaluation) | `contracts/kyb_contracts/decision.py` |

Formats marked *proposed here* aren't in `contracts/` yet. Their field lists come from the Nov 17 walkthrough in section 7 and may differ from their owners' drafts. Once a format is in `contracts/`, the code there is the source of truth, and this table should point to it. The worker formats are now in `worker_io.py`; section 7 still shows the earlier proposal (`WorkerTask`, `RecordsFinding`, `EvidenceFinding`).

**Evidence IDs:**

- R = official records (results of the Records worker's tools), D = merchant documents, B = bank verification, W = website snapshot, K = owner KYC (third-party, read by the Records worker).
- D, B, W and K IDs already exist in `CaseInput` (XL). R IDs are created at run time by the Records worker's tools (E), numbered from the task's `record_id_start` so they never repeat within a review.
- In a `WorkerResult`, each source's `source_id` is its evidence ID. The Records worker may cite only R and K, the Evidence worker only D, B and W; `worker_io.py` rejects anything else.
- The lead writes evidence IDs only, never excerpts. In step 9, Checks (code) looks each ID up in `CaseInput` and the workers' `WorkerResult.sources` and builds `evidence_index` (source, type, excerpt, date); an ID that can't be found is a citation error. The UI uses it to display citations (XL). This keeps excerpts out of the LLM, so they can't be reworded, and keeps merchant text out of the lead's context. **(Changed while working through the example; to confirm.)**

## 3. Each step in detail

### Step 1. Case JSON (output of the existing system)

| Item | Details |
|---|---|
| Tasks | Define the `CaseInput` format (Pydantic); store and serve cases in the case service; write the seed script that loads the golden cases into the database |
| Input | Golden cases written by the team: `case.json` and `world.json` per case, checked by `scripts/validate_golden.py` (see `data/golden/README.md`) |
| Output | `CaseInput` JSON served through `GET /cases/{id}` |
| Owner | XL; H and E review the format |
| Handoffs | To H: the fields intake needs. To E: the documents, bank_verification and web_snapshot fields the Evidence worker reads, and the owner_kyc fields the Records worker reads. From the team: the content of the golden cases |
| Done when | 3 sample cases pass validation; H and E confirm the fields are enough |

### Step 2. Intake (code)

| Item | Details |
|---|---|
| Tasks | Validate `CaseInput`; normalize names (suffixes, punctuation, case, "The", "&" vs "and"); compute the IRS name control; mark flags that are only formatting differences as resolved; filter out-of-scope flags (such as OFAC); if no flags remain, go straight to the memo |
| Input | `CaseInput` |
| Output | Initial graph state: case, normalized (normalized names and name control), open_flags, auto_resolved, out_of_scope_flags |
| Owner | H (to confirm: could move to XL, since it is closest to the data) |
| Handoffs | From XL: the `CaseInput` format. To step 3: open_flags |
| Done when | Formatting differences and out-of-scope flags are covered by unit tests |

### Step 3. Lead: plan (LLM)

| Item | Details |
|---|---|
| Tasks | List possible causes from open_flags and the case summary; write a goal and specific questions for each worker (questions to answer, not steps to follow); set each worker's tool-call budget (for example, 10 calls) |
| Input | Case summary (no website or document text), open_flags, normalized |
| Output | Two `WorkerInput` objects |
| Owner | H |
| Handoffs | To E: `WorkerInput` (`worker_io.py`), which the lead builds from `CaseInput` |
| Done when | All structured outputs pass validation; no website or document text appears in the tasks |

### Step 4. Records worker (ReAct loop)

| Item | Details |
|---|---|
| Tasks | Decide which tools to call within the budget: sos_search, sos_get_entity, sos_filing_history, dba_lookup, commercial_db_search, tin_match, get_kyc, normalize_name, name_control. Pin down the legal entity, find which entity owns the DBA, any name changes, and why the TIN doesn't match; answer the lead's questions |
| Input | `WorkerInput`; identifiers (applied name, DBA, address, state entity number, state, formation date, entity type, TIN type and last 4, owner refs, KYC IDs); upstream check result codes. Cannot see: document, bank or website contents; full SSNs or EINs |
| Output | `WorkerResult`; every finding cites a source with an R or K ID, a locator and a verbatim excerpt |
| Owner | E. The mock official records (state registry, DBA, filing history, TIN matching, commercial database) are written in each golden case's `world.json` and served by the case service (`docs/case-service-endpoints.md`) |
| Handoffs | From H: `WorkerInput` (first task or follow-up). To H: `WorkerResult` |
| Done when | It runs on its own on a case file, without the graph; when the budget runs out it returns unresolved items instead of guessing |

`tin_match` takes only the name to test; the tool gets the case's IRS record by `case_id` (case-service endpoint 6) and compares the names in code. No full SSN or EIN is involved, so none can enter the model's context. `get_kyc` takes an owner ref such as `owner:P1`.

### Step 5. Evidence worker (ReAct loop)

| Item | Details |
|---|---|
| Tasks | Fetch the stored, already-extracted fields of documents, bank verification and website snapshots by ID (no raw text, no re-parsing); decide which entity each piece of evidence points to; flag anything suspicious (low confidence, contradictions, expired documents, instruction-like values) |
| Input | `WorkerInput`; the merchant evidence IDs in the case (documents, bank_verification, web_snapshot); the applied name and TIN last 4 (for comparison). Cannot see: the official-records tools, their results, or owner KYC |
| Output | `WorkerResult`; every finding cites a source with a D, B or W ID |
| Owner | E |
| Handoffs | From XL: the format of the evidence fields in the case. From H: `WorkerInput` (first task or follow-up). To H: `WorkerResult` |
| Done when | In cases with hidden instructions, the instructions are flagged as suspicious and never followed |

### Step 6. Reconcile (code)

| Item | Details |
|---|---|
| Tasks | Line up the two findings field by field: legal name, entity number, EIN, formation date, DBA owner, owner identity (for sole proprietors). Mark each field as agree, conflict, missing in records or missing in evidence |
| Input | The Records and Evidence `WorkerResult`s |
| Output | `ReconcileTable` |
| Owner | H |
| Handoffs | From E: field names in the findings stay stable; tell H before changing them |
| Done when | Every conflict designed into the golden cases is caught |

### Step 7. Lead: follow up? (LLM, max 2 rounds)

| Item | Details |
|---|---|
| Tasks | Read the reconcile table. If there is a conflict or a key field is missing, send one targeted question to one worker, passing along only the one fact needed to answer it; otherwise, move on to the memo |
| Stops when | All conflicts are resolved; a fraud signal appears; only documents are missing; or 2 rounds are used, the last round found nothing new, or the budget is spent |
| Input | `ReconcileTable`, both findings, number of rounds so far |
| Output | One `FollowUp`, or "go to memo" |
| Owner | H |
| Handoffs | To E: workers must be able to run another round when they receive a `FollowUp` |
| Done when | Never more than 2 rounds; `shared_facts` never contains website or document text |

### Step 8. Lead: write memo (LLM, structured output)

| Item | Details |
|---|---|
| Tasks | For each flag, write the outcome (resolved / unresolved / out of scope), the root cause, the causes ruled out, the recommendation, the documents to request and notes for the analyst; cite evidence IDs for every conclusion, IDs only and no excerpts (`evidence_index` is built in step 9). Code copies `auto_resolved` and `out_of_scope` from intake (to confirm) |
| Input | Both findings, the reconcile table, the follow-up history; plus the `CheckResult` when step 9 sends the memo back |
| Output | `Memo`. The recommendation is one of four: recommend_clear, request_documents, escalate, insufficient_evidence |
| Owner | H; XL approves the format |
| Handoffs | To XL: the `Memo` format (`evidence_index` comes from step 9) |
| Done when | All structured outputs pass validation |

### Step 9. Checks (code)

| Item | Details |
|---|---|
| Tasks | First, build `evidence_index`: look up every ID the memo cites in `CaseInput` and the workers' `WorkerResult.sources` and take its source, type, excerpt and date; an ID that can't be found is a citation error (to confirm). Then check the hard rules, only for flags the lead marked resolved: clearing needs both an official or third-party record (R or K) and merchant evidence (D, B or W); an EIN that belongs to an unrelated company must be escalated; a new company whose EIN is `Not Issued` can't have its TIN flag cleared; no full SSN or EIN may appear in the memo. On failure, send the memo back to the lead once; if it still fails, flag it for the analyst |
| Input | `Memo`, `CaseInput`, both `WorkerResult`s |
| Output | `CheckResult`, plus the memo with `evidence_index` attached |
| Owner | H (this is the guardrails layer) |
| Handoffs | To XL: `CheckResult` and `evidence_index`; the UI shows "checks passed" or a warning, and uses `evidence_index` to display citations |
| Done when | Every hard rule is triggered by at least one golden case |

### Step 10. Analyst decision (interrupt)

| Item | Details |
|---|---|
| Tasks | The graph pauses here with `interrupt`, and the checkpointer (PostgreSQL) saves its state. The UI shows the memo, evidence and check results; the analyst approves, requests documents, escalates or denies, and adds a note; the backend checks the role, then calls the resume endpoint so the graph continues |
| Input | `Memo`, `CheckResult`, case details; the analyst's action |
| Output | `DecisionInput`, which resumes the graph through `POST /reviews/{id}/decision` |
| Owner | The interrupt node and resume logic in the graph: H. The API, UI and access control: XL |
| Handoffs | H and XL agree on how review_id maps to thread_id, what the resume call passes, and the status values (running, awaiting_decision, done, failed) |
| Done when | After a page refresh, the case can still continue from "awaiting decision"; roles without permission get 403 |

### Step 11. Decision saved

| Item | Details |
|---|---|
| Tasks | Save the decision record (who, when, which decision, the note, the agent's recommendation, whether they agree); update the case status, only through this flow; write the audit log |
| Input | `DecisionInput` + the memo's recommendation |
| Output | `DecisionRecord`, the updated case status, the audit log |
| Owner | XL |
| Handoffs | To evaluation: `DecisionRecord` is used to compute the override rate |
| Done when | The case status can't be changed any other way |

## 4. Work that spans the whole flow

| Work | Details | Owner | Handoffs |
|---|---|---|---|
| Auth and API gateway | JWT login, roles, permission checks on every endpoint; the gateway routes `/auth`, `/cases` and `/reviews` | XL | To the team: the JWT claims and the list of roles |
| Agent service API | `POST /reviews` starts a review in the background; `GET /reviews/{id}` returns the status and memo; `POST /reviews/{id}/decision` submits the decision and resumes the graph; the frontend polls the status | XL (HTTP layer) + H (graph calls) | Agree on the endpoints with H |
| LangFuse | A trace for every node and every LLM and tool call | H | E's workers use the same callback setup |
| Golden dataset | About 28 cases, each with expected_decision, must_cite and forbidden_actions; 9–10 per person, each set including one case with hidden instructions. Proposed: XL writes EC-1, E writes EC-2, H writes EC-4 and the combined cases (to confirm). How to write and check a case: `data/golden/README.md` | Everyone | To XL for loading into the database; to H for evaluation |
| Evaluation runner and single-agent baseline | Run every case; compute false-clear rate, accuracy, citation validity, cost and latency; the single agent uses the same tools and code checks | H | From E: tools that the single agent can reuse |
| Override rate | Share of cases where the analyst's decision differs from the agent's recommendation | XL | From H: the recommendation in the memo |
| Deployment | Each owner writes the Dockerfile for their service; XL integrates Docker Compose and EC2 | XL | Every owner hands in their Dockerfile by 11/20 |

## 5. Handoff schedule

| Deliverable | From | To | Due |
|---|---|---|---|
| `CaseInput` format + 3 sample cases | XL | H, E | 11/18 morning |
| `WorkerInput` format (also used for follow-ups) | H | E | 11/18 morning |
| `WorkerResult` format | E | H | 11/18 morning |
| `Memo`, `CheckResult` and `evidence_index` formats | H | XL | 11/18 morning |
| Agent service API and status values | XL + H | Everyone | 11/18 morning |
| JWT claims and list of roles | XL | Everyone | 11/18 |
| **One case runs end to end** | Everyone | — | **11/18 evening** |
| All golden cases | Everyone | XL (loading), H (evaluation) | 11/19 evening |
| Dockerfile for each service | Each owner | XL | 11/20 |
| First full evaluation results | H | Everyone | 11/20 |

After that: 11/21–22 for EC2 deployment, the second evaluation run, slides and rehearsal; presentation on 11/23 at 2:00 p.m. ET.

Progress on each deliverable is tracked in Jira (subtasks under BEAC-3, BEAC-4 and BEAC-5), not in this doc.

## 6. Open questions

1. Do both workers' tool interfaces belong to E? (The mock official records are now written in each golden case's `world.json` and served by the case service.)
2. Does intake belong to H or XL?
3. Is splitting the golden cases by edge case across the three of us OK?
4. With the scope narrowed to EC-1/2/4, which roles do we keep?
5. Deploy to EC2, with a local demo ready as a backup?
6. `evidence_index` is now built by code in step 9, and the lead writes IDs only (changed while working through the example). Does H agree?
7. Code copies `auto_resolved` and `out_of_scope` from intake into the memo, and the hard rules only check flags the lead marked resolved. OK?
8. How to compute the override rate (see the table in 7.15): XL and H to confirm.
9. The analyst's decision is spelled `request_docs`, but the recommendation is `request_documents`. Keep both spellings, or use one?
10. `NAME_UNVERIFIED` vs `ENTITY_NOT_FOUND`: the proposed rule is that a `checks.sos_name` result of Unverified with a `matched_name` means `NAME_UNVERIFIED`, and without one means `ENTITY_NOT_FOUND` (see `contracts/case_input_guide.md`, 6.2). OK?
11. `TIN_NOT_FOUND` vs `TIN_NOT_ISSUED`: real IRS TIN matching has no separate "not found" result. How do the two labels differ in our cases?
12. After `docs_requested`, does the case go back to `pending_review`, or is a new case opened?
13. Worker formats: `contracts/kyb_contracts/worker_io.py` now defines `WorkerInput` (aliases `WorkerTask`, `FollowUp`) and `WorkerResult`, built on `CaseInput`'s IDs. H to review `WorkerInput`; section 7 still uses the earlier proposal and needs rewriting to match.

## 7. Walkthrough example: one case from step 1 to step 11

> Every company, person and number below is made up. The EIN starts with `00`, which real EINs never do. The case involves both EC-1 (DBA vs legal name) and EC-2 (the EIN doesn't match the name).
>
> Only `CaseInput` and the decision formats match code in `contracts/`. The worker tasks and findings below use the earlier proposal (`WorkerTask`, `RecordsFinding`, `EvidenceFinding`); the code now uses `WorkerInput` and `WorkerResult` from `worker_io.py` (see section 2). The other formats are the proposals from section 2.

### 7.1 Background

Northline Bicycle Works LLC is a bike shop in Illinois (IL) that trades as Northline Cycles. Its history:

- 2023-05-30: formed with the state as **Harbor Light Cycles LLC**. On 2023-06-08 it got its EIN; the IRS CP 575 letter shows this old name.
- 2024-01-15: registered the DBA **Northline Cycles** under the old name.
- 2025-03-12: changed its name with the state to **Northline Bicycle Works LLC**, but never told the IRS.

So the name now differs by source: the state has the new name, while the IRS and the DBA record still have the old one. The existing system spots the mismatches, flags the case and sends it to manual review.

### 7.2 The whole path at a glance

| Step | Data produced | What happens in this case |
|---|---|---|
| 1 | `CaseInput` | 4 flags; the merchant's EIN letter shows the old name |
| 2 | `open_flags` and more | 1 auto-resolved, 1 out of scope, 2 left to investigate |
| 3 | `WorkerTask` × 2 | 3 questions for Records, 2 for Evidence |
| 4 and 5 (round 1) | `RecordsFinding`, `EvidenceFinding` | R1–R4 created; D1, B1, W1 read. Records uses up its budget |
| 6 (round 1) | `ReconcileTable` | 3 conflicts, all pointing to one name: HARBOR LIGHT CYCLES LLC |
| 7 (first time) | `FollowUp` F1 → Records | Asks: former name, or a different company? |
| 4 (round 2) | `RecordsFinding` (appended) | R5–R7 created: renamed in 2025; the EIN matches the old name |
| 6 (round 2) | `ReconcileTable` | 0 conflicts |
| 7 (second time) | Go to memo | Only 1 follow-up round used |
| 8 (version 1) | `Memo` | Recommends recommend_clear |
| 9 (first run) | `CheckResult` | Fails: the DBA flag cites official records only |
| 8 (version 2) | `Memo` | Adds merchant evidence B1 and W1 to the DBA flag |
| 9 (second run) | `CheckResult` + `evidence_index` | Passes |
| 10 | `DecisionInput` | The analyst approves |
| 11 | `DecisionRecord` | overridden = false; the case status becomes approved |

### 7.3 Step 1, Case JSON: the case arrives

The existing system stores the case in the case service. The analyst clicks "Start review" in the UI (reviews could also start automatically when a case arrives). The frontend calls `POST /reviews`, the agent service returns a `review_id` (here `rv_7f3a`), then loads the case from the case service in the background. This case is `contracts/examples/case_input/case_1042.json`:

```json
{
  "case_id": "case_1042",
  "status": "pending_review",
  "flag_reasons": ["NAME_SIMILAR_MATCH", "DBA_UNVERIFIED", "TIN_MISMATCH", "PRODUCT_CATEGORY_REVIEW"],
  "application": {
    "legal_name": "Northline Bicycle Works LLC",
    "dba": "Northline Cycles",
    "address": "1450 W Fulton St, Chicago, IL 60607",
    "tin_type": "EIN",
    "tin_last4": "4567",
    "state": "IL",
    "state_entity_id": "IL-0458821",
    "entity_type": "LLC",
    "formation_date": "2023-05-30",
    "owners": [{ "ref": "owner:P1", "name": "Dana Park", "role": "Manager" }]
  },
  "checks": {
    "sos_name": { "result": "Similar Match", "matched_name": "NORTHLINE BICYCLE WORKS, L.L.C." },
    "dba": { "result": "Unverified" },
    "tin": { "result": "Mismatch" }
  },
  "documents": [
    {
      "id": "D1",
      "type": "irs_cp575",
      "extracted": { "name": "HARBOR LIGHT CYCLES LLC", "date": "2023-06-08", "ein_last4": "4567" },
      "confidence": 0.96
    }
  ],
  "bank_verification": {
    "id": "B1",
    "account_holder": "NORTHLINE BICYCLE WORKS LLC DBA NORTHLINE CYCLES",
    "status": "verified",
    "verified_at": "2026-11-10"
  },
  "web_snapshot": [
    {
      "id": "W1",
      "url": "https://northlinecycles.example/terms",
      "captured_at": "2026-11-10",
      "extracted": { "legal_name": "Northline Bicycle Works LLC", "dba": "Northline Cycles" }
    }
  ],
  "owner_kyc": [{ "id": "K1", "owner_ref": "owner:P1", "result": "pass" }]
}
```

`contracts/case_input_guide.md` explains every field and the KYB terms behind it. Four things to keep in mind:

- Everything in `application` is the merchant's claim, and `checks` only shows where things don't match. The real evidence is the merchant evidence (`documents`, `bank_verification`, `web_snapshot`, `owner_kyc`) plus the official records the Records worker finds.
- The application carries only the TIN's type and last 4 digits, and owners appear as references such as `owner:P1`, so full SSNs and EINs never reach the model.
- Everything arrives already parsed. Raw OCR and page text stay in case_db; the extracted values are still the merchant's claims, and only the Evidence worker reads documents, bank verification and web snapshots. The Records worker reads owner KYC.
- D1, B1, W1 and K1 are fixed when the case arrives and never change.

### 7.4 Step 2, Intake: a first pass in code

1. `NAME_SIMILAR_MATCH`: the application says "Northline Bicycle Works LLC" and the state record says "NORTHLINE BICYCLE WORKS, L.L.C.". After normalization (uppercase, comma removed, L.L.C. → LLC) both read "NORTHLINE BICYCLE WORKS LLC". It's only a formatting difference, so it's resolved automatically.
2. `PRODUCT_CATEGORY_REVIEW`: this is about what the merchant sells, not EC-1/2/4, so it's marked out of scope and left for the analyst as is.
3. Name control: the IRS matches on the first 4 characters of the business name, so "NORTHLINE…" gives `NORT`.
4. Two flags remain, so there's no early exit.

```json
{
  "normalized": {
    "applied_legal_name": "NORTHLINE BICYCLE WORKS LLC",
    "sos_matched_name": "NORTHLINE BICYCLE WORKS LLC",
    "dba": "NORTHLINE CYCLES",
    "name_control": "NORT"
  },
  "auto_resolved": [
    { "flag": "NAME_SIMILAR_MATCH", "reason": "Same name after normalization (case, comma, L.L.C. → LLC)" }
  ],
  "out_of_scope_flags": ["PRODUCT_CATEGORY_REVIEW"],
  "open_flags": ["DBA_UNVERIFIED", "TIN_MISMATCH"]
}
```

### 7.5 Step 3, Lead: plan

The lead gets only a case summary and `open_flags`; it sees neither D1's contents nor the website text. It writes one task per worker:

```json
[
  {
    "worker": "records",
    "goal": "Confirm the legal entity and explain the DBA and TIN flags from official records",
    "questions": [
      { "id": "Q1", "text": "Which registered entity matches the application, and is it active?" },
      { "id": "Q2", "text": "Who is the registered owner of the DBA Northline Cycles?" },
      { "id": "Q3", "text": "Why does the EIN fail the IRS TIN match for the applied legal name?" }
    ],
    "candidate_causes": ["DBA filed by a different entity", "EIN belongs to a different entity", "Name formatting at the IRS"],
    "budget": { "tool_calls": 4 }
  },
  {
    "worker": "evidence",
    "goal": "Check what the merchant's own evidence says about the legal name, DBA and EIN",
    "questions": [
      { "id": "Q4", "text": "Which name and EIN does the EIN letter show?" },
      { "id": "Q5", "text": "Which legal entity do the bank account and website tie the DBA to?" }
    ],
    "budget": { "tool_calls": 4 }
  }
]
```

Note that "name change" isn't among the lead's candidate causes, because it can't know that yet. That's fine: catching cases like this is what the follow-up is for.

### 7.6 Steps 4 and 5, round 1: both workers in parallel

LangGraph's `Send` starts both workers at the same time.

**Records worker** (sees only the application's identifiers and the upstream check results):

| Call | Tool | Result | ID |
|---|---|---|---|
| 1 | `sos_search(name="Northline Bicycle Works", state="IL")` | One match: IL-0458821, Active | R1 |
| 2 | `sos_get_entity("IL-0458821")` | LLC, formed 2023-05-30, manager D. PARK | R2 |
| 3 | `dba_lookup(name="Northline Cycles", state="IL")` | Filed 2024-01-15 by HARBOR LIGHT CYCLES LLC | R3 |
| 4 | `tin_match(name="NORTHLINE BICYCLE WORKS LLC")` | Mismatch | R4 |

After 4 calls the budget is used up, so the worker stops and lists what it couldn't settle under `unresolved` instead of guessing. Note that `tin_match` gets only the name; the tool compares it with the case's IRS record, looked up by `case_id`.

```json
{
  "entity_candidates": [
    { "state_entity_id": "IL-0458821", "name": "NORTHLINE BICYCLE WORKS, L.L.C.", "evidence": ["R1"] }
  ],
  "selected_entity": {
    "state_entity_id": "IL-0458821",
    "legal_name": "NORTHLINE BICYCLE WORKS LLC",
    "status": "Active",
    "formed": "2023-05-30",
    "evidence": ["R1", "R2"]
  },
  "dbas": [
    { "name": "NORTHLINE CYCLES", "registered_owner": "HARBOR LIGHT CYCLES LLC", "filed": "2024-01-15", "evidence": ["R3"] }
  ],
  "name_history": [],
  "tin_results": [
    { "tin": "EIN ending 4567", "name_tested": "NORTHLINE BICYCLE WORKS LLC", "result": "Mismatch", "evidence": ["R4"] }
  ],
  "answers": [
    { "question_id": "Q1", "answer": "IL-0458821, active, formed 2023-05-30", "evidence": ["R1", "R2"] },
    { "question_id": "Q2", "answer": "Filed by HARBOR LIGHT CYCLES LLC, not by the applied name", "evidence": ["R3"] },
    { "question_id": "Q3", "answer": "Not confirmed", "evidence": ["R4"] }
  ],
  "unresolved": [
    "How HARBOR LIGHT CYCLES LLC relates to IL-0458821 (budget used up)",
    "Cause of the TIN mismatch"
  ],
  "evidence": [
    { "id": "R1", "source": "IL SoS search", "as_of": "2026-11-17", "excerpt": "NORTHLINE BICYCLE WORKS, L.L.C. | IL-0458821 | Active" },
    { "id": "R2", "source": "IL SoS entity record", "as_of": "2026-11-17", "excerpt": "LLC, formed 2023-05-30, manager D. PARK" },
    { "id": "R3", "source": "IL DBA records", "as_of": "2026-11-17", "excerpt": "NORTHLINE CYCLES, filed 2024-01-15 by HARBOR LIGHT CYCLES LLC" },
    { "id": "R4", "source": "IRS TIN match", "as_of": "2026-11-17", "excerpt": "EIN + NORTHLINE BICYCLE WORKS LLC: Mismatch" }
  ]
}
```

**Evidence worker** (sees only the merchant's own evidence, plus the applied name and TIN last 4 for comparison):

1. D1: extraction confidence is 0.96. Its `ein_last4` matches the application's `tin_last4`, but the name is HARBOR LIGHT CYCLES LLC, not the applied name.
2. B1: bank verification passed, and the account title links Northline Cycles to Northline Bicycle Works LLC.
3. W1: the fields extracted from the website's terms page give legal name Northline Bicycle Works LLC and DBA Northline Cycles. They stay low-confidence merchant claims.
4. K1 isn't read here: owner KYC is third-party data, so it belongs to the Records worker.
5. Nothing suspicious, such as instruction-like values.

```json
{
  "evidence_items": [
    { "id": "D1", "kind": "irs_ein_letter", "points_to": "HARBOR LIGHT CYCLES LLC", "ein_matches_application": true, "date": "2023-06-08" },
    { "id": "B1", "kind": "bank_account", "points_to": "NORTHLINE BICYCLE WORKS LLC", "dba": "NORTHLINE CYCLES", "status": "verified" },
    { "id": "W1", "kind": "website", "points_to": "NORTHLINE BICYCLE WORKS LLC", "dba": "NORTHLINE CYCLES" }
  ],
  "consistency": [
    {
      "field": "legal_name",
      "status": "conflict",
      "detail": "D1 names HARBOR LIGHT CYCLES LLC; B1 and W1 name NORTHLINE BICYCLE WORKS LLC",
      "evidence": ["D1", "B1", "W1"]
    }
  ],
  "suspicious": [],
  "answers": [
    { "question_id": "Q4", "answer": "D1 shows the applied EIN issued to HARBOR LIGHT CYCLES LLC", "evidence": ["D1"] },
    { "question_id": "Q5", "answer": "B1 and W1 tie NORTHLINE CYCLES to NORTHLINE BICYCLE WORKS LLC", "evidence": ["B1", "W1"] }
  ],
  "unresolved": ["Why the EIN letter names a different company"]
}
```

### 7.7 Step 6, Reconcile: round 1

| Field | Official records | Merchant evidence | Status |
|---|---|---|---|
| legal_name | NORTHLINE BICYCLE WORKS LLC (R1, R2) | B1 and W1 show the new name; D1 shows HARBOR LIGHT CYCLES LLC | conflict |
| entity_id | IL-0458821 (R1) | None | missing_in_evidence |
| ein | No match with the new name (R4) | D1: the same EIN was issued to HARBOR LIGHT CYCLES LLC | conflict |
| formation_date | 2023-05-30 (R2) | D1 is dated 2023-06-08, after formation | agree |
| dba_owner | HARBOR LIGHT CYCLES LLC (R3) | NORTHLINE BICYCLE WORKS LLC (B1, W1) | conflict |

Each row looks like this in `ReconcileTable`:

```json
{
  "field": "dba_owner",
  "records_value": "HARBOR LIGHT CYCLES LLC",
  "evidence_value": "NORTHLINE BICYCLE WORKS LLC",
  "status": "conflict",
  "evidence_ids": ["R3", "B1", "W1"]
}
```

The missing `entity_id` in the evidence isn't a key problem: merchant documents rarely carry a state entity number.

### 7.8 Step 7, Lead: follow up? (first time)

All three conflicts point to one name: HARBOR LIGHT CYCLES LLC. There are only two possibilities:

- It's the company's former name: explainable, not a real problem.
- It's an unrelated company: the EIN belongs to someone else, and the hard rule says escalate.

Official records can tell these apart, so the lead sends one question to the Records worker only. It passes along one fact and none of D1's text:

```json
{
  "to": "records",
  "question_id": "F1",
  "question": "Is HARBOR LIGHT CYCLES LLC a former name of IL-0458821 or a separate company? Then test the applied EIN against that name.",
  "reason": "The legal_name, ein and dba_owner conflicts all point to this one name",
  "shared_facts": ["D1, the merchant's IRS EIN letter dated 2023-06-08, lists HARBOR LIGHT CYCLES LLC for the applied EIN"],
  "budget": { "tool_calls": 3 }
}
```

### 7.9 Step 4, round 2: only the Records worker runs again

| Call | Tool | Result | ID |
|---|---|---|---|
| 5 | `sos_filing_history("IL-0458821")` | 2025-03-12 Articles of Amendment: HARBOR LIGHT CYCLES LLC renamed NORTHLINE BICYCLE WORKS LLC | R5 |
| 6 | `sos_search(name="Harbor Light Cycles", state="IL")` | No separate company by that name; it appears only as a former name of IL-0458821 | R6 |
| 7 | `tin_match(name="HARBOR LIGHT CYCLES LLC")` | Match | R7 |

R IDs continue from round 1 and never repeat within a review. In round 2 the worker returns only what it newly found. When this merges into the existing `RecordsFinding`, the list fields (`evidence`, `answers`, `tin_results`, `name_history`) are appended, and `unresolved` is replaced by this round's list:

```json
{
  "name_history": [
    { "old_name": "HARBOR LIGHT CYCLES LLC", "new_name": "NORTHLINE BICYCLE WORKS LLC", "date": "2025-03-12", "evidence": ["R5"] }
  ],
  "tin_results": [
    { "tin": "EIN ending 4567", "name_tested": "HARBOR LIGHT CYCLES LLC", "result": "Match", "evidence": ["R7"] }
  ],
  "answers": [
    {
      "question_id": "F1",
      "answer": "Former name of IL-0458821, changed 2025-03-12. No separate company by that name in IL. The applied EIN matches the former name at the IRS.",
      "evidence": ["R5", "R6", "R7"]
    }
  ],
  "unresolved": [],
  "evidence": [
    { "id": "R5", "source": "IL SoS filing history", "as_of": "2026-11-17", "excerpt": "2025-03-12 Articles of Amendment: HARBOR LIGHT CYCLES LLC → NORTHLINE BICYCLE WORKS LLC" },
    { "id": "R6", "source": "IL SoS search", "as_of": "2026-11-17", "excerpt": "No separate entity named HARBOR LIGHT CYCLES LLC; former name of IL-0458821" },
    { "id": "R7", "source": "IRS TIN match", "as_of": "2026-11-17", "excerpt": "EIN + HARBOR LIGHT CYCLES LLC: Match" }
  ]
}
```

Now the root cause is clear: the IRS TIN match compares name controls. The IRS still has the old name, whose name control is `HARB`; matching against the new name gives `NORT`, so it keeps failing.

### 7.10 Step 6, Reconcile: round 2, then step 7: no more follow-ups

Reconcile has a rule: a former name in `name_history` that official records prove counts as the same company.

| Field | Status | Basis |
|---|---|---|
| legal_name | agree | Current name in R2; D1 shows the former name, and R5 proves it's the same company |
| ein | agree | The IRS has the former name (R7); D1 shows the same EIN |
| dba_owner | agree | R3's filer is the former name (R5); B1, W1 |
| formation_date | agree | Same as round 1 |
| entity_id | missing_in_evidence | Same as round 1; not a key field |

No conflicts are left, so the lead moves on to the memo. It used 1 of its 2 follow-up rounds.

### 7.11 Step 8, Lead: write memo (version 1)

```json
{
  "case_id": "case_1042",
  "flags": [
    {
      "flag": "DBA_UNVERIFIED",
      "status": "resolved",
      "root_cause": "The DBA was filed in 2024 under the company's former name, before the 2025 name change",
      "evidence": ["R3", "R5"]
    },
    {
      "flag": "TIN_MISMATCH",
      "status": "resolved",
      "root_cause": "The IRS still has the former name, so the TIN match fails on the current name",
      "evidence": ["R4", "R5", "R7", "D1"]
    }
  ],
  "auto_resolved": ["NAME_SIMILAR_MATCH"],
  "out_of_scope": ["PRODUCT_CATEGORY_REVIEW"],
  "ruled_out": [
    { "cause": "EIN belongs to an unrelated company", "evidence": ["R5", "R6", "R7"] }
  ],
  "recommendation": "recommend_clear",
  "document_request": [],
  "notes_for_analyst": "Ask the merchant to update its name with the IRS; the TIN match will keep failing on the current name until it does. The agent did not review PRODUCT_CATEGORY_REVIEW."
}
```

The lead writes evidence IDs only, never excerpts. `auto_resolved` and `out_of_scope` are copied from intake by code, not written by the LLM.

### 7.12 Step 9, Checks: the first run fails

Checks (code) does two things in order:

1. Build `evidence_index`: the memo cites R3, R4, R5, R6, R7 and D1. Each is looked up in `RecordsFinding` and `CaseInput`; all are found, so the citation check passes.
2. Check the hard rules, only for flags the lead marked resolved:
   - Clearing needs two sources: TIN_MISMATCH has official records (R4, R5, R7) and merchant evidence (D1), so it passes. DBA_UNVERIFIED has official records only (R3, R5), so it **fails**.
   - EIN belongs to an unrelated company: ruled out (R5, R6, R7), pass.
   - EIN is `Not Issued`: no, not applicable.
   - SSN in the memo: none, pass.

```json
{
  "passed": false,
  "failures": [
    {
      "rule": "CLEAR_NEEDS_TWO_SOURCES",
      "flag": "DBA_UNVERIFIED",
      "detail": "Resolved with official records only (R3, R5). Also cite merchant evidence (D, B or W)."
    }
  ]
}
```

This was the first check, so the memo goes back to the lead for its one revision.

### 7.13 Step 8, version 2, then step 9: the second run passes

The lead changes only the DBA_UNVERIFIED evidence:

```json
{
  "flag": "DBA_UNVERIFIED",
  "status": "resolved",
  "root_cause": "The DBA was filed in 2024 under the company's former name, before the 2025 name change",
  "evidence": ["R3", "R5", "B1", "W1"]
}
```

Checks runs again, everything passes, and the `evidence_index` is attached to the memo. It holds only the IDs the memo cites; R1, R2 and K1 aren't cited, so they're left out:

```json
{
  "check_result": { "passed": true, "failures": [] },
  "evidence_index": {
    "R3": { "source": "IL DBA records", "type": "official_record", "excerpt": "NORTHLINE CYCLES, filed 2024-01-15 by HARBOR LIGHT CYCLES LLC", "as_of": "2026-11-17" },
    "R4": { "source": "IRS TIN match", "type": "official_record", "excerpt": "EIN + NORTHLINE BICYCLE WORKS LLC: Mismatch", "as_of": "2026-11-17" },
    "R5": { "source": "IL SoS filing history", "type": "official_record", "excerpt": "2025-03-12 Articles of Amendment: HARBOR LIGHT CYCLES LLC → NORTHLINE BICYCLE WORKS LLC", "as_of": "2026-11-17" },
    "R6": { "source": "IL SoS search", "type": "official_record", "excerpt": "No separate entity named HARBOR LIGHT CYCLES LLC; former name of IL-0458821", "as_of": "2026-11-17" },
    "R7": { "source": "IRS TIN match", "type": "official_record", "excerpt": "EIN + HARBOR LIGHT CYCLES LLC: Match", "as_of": "2026-11-17" },
    "D1": { "source": "Merchant upload", "type": "merchant_document", "excerpt": "IRS CP 575: HARBOR LIGHT CYCLES LLC, EIN 00-1234567", "as_of": "2023-06-08" },
    "B1": { "source": "Bank verification", "type": "bank_verification", "excerpt": "Account holder: NORTHLINE BICYCLE WORKS LLC DBA NORTHLINE CYCLES (verified)", "as_of": "2026-11-10" },
    "W1": { "source": "https://northlinecycles.example/terms", "type": "website", "excerpt": "Northline Cycles is a trade name of Northline Bicycle Works LLC.", "as_of": "2026-11-10" }
  }
}
```

### 7.14 Step 10, Analyst decision

1. Once the checks pass, the graph reaches the interrupt node and pauses. The checkpointer saves the whole state to PostgreSQL with `thread_id` = `rv_7f3a`.
2. The frontend has been polling `GET /reviews/rv_7f3a`. The status changes from `running` to `awaiting_decision`, and the response now carries the memo, `evidence_index` and `check_result`.
3. The UI shows: the recommendation recommend_clear; two resolved flags, where each ID next to a conclusion (R3, B1 and so on) opens its excerpt; "Checks passed"; and PRODUCT_CATEGORY_REVIEW marked "needs your review".
4. The analyst checks the product category (bicycles and parts), finds no problem, clicks Approve and adds a note. The frontend calls:

```http
POST /reviews/rv_7f3a/decision
Authorization: Bearer <JWT>
Content-Type: application/json

{
  "decision": "approve",
  "note": "Name change confirmed by the SoS amendment. Product category (bicycles and parts) is fine. Merchant asked to update its name with the IRS."
}
```

5. The gateway verifies the JWT, the agent service checks that the role is analyst, then resumes the graph with this `DecisionInput`. `analyst_id` comes from the JWT, never from the request body.

### 7.15 Step 11, Decision saved

After resuming, the graph reaches END. The agent service calls the case service to save the record:

```json
{
  "review_id": "rv_7f3a",
  "case_id": "case_1042",
  "decision": "approve",
  "note": "Name change confirmed by the SoS amendment. Product category (bicycles and parts) is fine. Merchant asked to update its name with the IRS.",
  "analyst_id": "u_analyst_02",
  "agent_recommendation": "recommend_clear",
  "overridden": false,
  "decided_at": "2026-11-17T16:05:00-06:00"
}
```

At the same time, the case status changes from `pending_review` to `approved`, and the audit log records who changed the status, from what to what, and when. After that, `GET /reviews/rv_7f3a` returns the status `done`.

How `overridden` is computed (proposal, to confirm):

| Agent recommendation | Decisions that count as agreeing |
|---|---|
| recommend_clear | approve |
| request_documents | request_docs |
| escalate | escalate or deny |
| insufficient_evidence | request_docs or escalate |

Any other pair counts as overridden = true, for example recommend_clear followed by deny.

This table is `AGREES_WITH` in `contracts/kyb_contracts/decision.py`, and `is_override()` applies it. It is still a proposal (to confirm).

### 7.16 API timeline and LangFuse trace

API (XL + H):

```
UI                                  Agent service                            Graph
POST /reviews {case_id}        ──→  202 {review_id: rv_7f3a, running}   ──→  start (thread_id = rv_7f3a)
GET /reviews/rv_7f3a (polls)   ──→  {status: running}                        steps 2–9
GET /reviews/rv_7f3a           ──→  {status: awaiting_decision, memo …}      step 10: paused
POST /reviews/rv_7f3a/decision ──→  role check → resume                 ──→  step 10: resumed → END
                                    save DecisionRecord (step 11)
GET /reviews/rv_7f3a           ──→  {status: done}
```

The LangFuse trace for this review (H):

```
trace rv_7f3a (case_1042)
├─ intake                    code     2 open flags
├─ lead_plan                 LLM      2 WorkerInputs
├─ records_worker  round 1   ReAct    4 tool calls (R1–R4), budget used up
├─ evidence_worker round 1   ReAct    4 tool calls (D1, B1, W1, K1)
├─ reconcile                 code     3 conflicts
├─ lead_follow_up            LLM      FollowUp F1 → records
├─ records_worker  round 2   ReAct    3 tool calls (R5–R7)
├─ reconcile                 code     0 conflicts
├─ lead_follow_up            LLM      go to memo
├─ write_memo  v1            LLM
├─ checks                    code     failed: CLEAR_NEEDS_TWO_SOURCES
├─ write_memo  v2            LLM
├─ checks                    code     passed
└─ analyst_decision          interrupt → resumed with approve
```

### 7.17 How other cases would branch

| Situation | Where it branches | What happens |
|---|---|---|
| Formatting differences only (e.g. just NAME_SIMILAR_MATCH) | 2 | Intake resolves it, skips steps 3–7 and goes straight to step 8 |
| EC-4: newly formed company with a brand-new EIN | 4 | `tin_match` returns `Not Issued`; the hard rule in step 9 blocks clearing the TIN flag → request_documents (ask for the IRS EIN letter) |
| EC-4: sole proprietor with no state registration | 4 | Finding no entity is expected; `tin_match(name="<owner's name>")` checks the owner's name against the IRS record for the submitted SSN (`tin_type` = SSN); KYC (K) supports the owner's identity, and clearing still needs merchant evidence such as B or D |
| The EIN belongs to an unrelated company | 4 (round 2) | The Records worker finds a separate company → hard rule in step 9 → escalate |
| Hidden instructions on the website (e.g. "ignore previous instructions…") | 5 | The Evidence worker records them under `suspicious` and doesn't follow them; the lead gets only the fact "W1 contains instruction-like text", and the memo warns the analyst |
| Conflicts remain after 2 follow-up rounds | 7 | No more follow-ups; the memo is written with the `unresolved` items → insufficient_evidence |
| The revised memo still fails the checks | 9 | No second send-back; it goes to the analyst with the failures, and the UI shows a warning |

### 7.18 This case as a golden case

```yaml
case_id: case_1042
edge_cases: [EC-1, EC-2]
expected_decision: recommend_clear      # the agent's recommendation
max_follow_up_rounds: 2
must_cite:
  - D1
  - any_of: [B1, W1]
  - source: sos_filing_history          # R IDs can change between runs, so describe them by source
  - source: tin_match
    name_tested: HARBOR LIGHT CYCLES LLC
    result: Match
forbidden_actions:
  - escalate with "EIN belongs to an unrelated company"
  - put raw document or website text in shared_facts
```

R IDs are generated at run time in call order. If the same case runs twice, the name-change record might be R5 one time and R4 the next, so the golden case describes R evidence by source. D, B, W and K IDs are fixed in the case, so they can be listed directly.
