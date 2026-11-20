# Case service: read endpoints the agent workers need

**From:** the sub-agents (Records and Evidence workers)
**To:** the case-service owner
**Status:** revised Nov 18 to match the contracts in `contracts/kyb_contracts/` (`case_input.py`, `worker_io.py`). Please reply with changes.

Everything is already parsed and stored by the case service: the application, the vendor checks, the fields extracted from each document, the bank verification, the website fields and the KYC results. The workers parse nothing. Each worker tool makes one `GET` call and fetches a stored record **by a key** the lead passes in the task: an evidence ID (`D1`, `B1`, `W1`, `K1`), the case's `state_entity_id`, or a name to search for.

These 10 endpoints are read-only, and only the agent service calls them. `GET /cases/{case_id}` (the whole `CaseInput`, used by intake and the lead) already exists and isn't repeated here.

## Common rules

- **Auth:** `Authorization: Bearer <service JWT>` with role `agent_service`. That role gets only these endpoints, `GET /cases/{case_id}`, and one write: `POST /cases/{case_id}/decisions` for step 11 (at the end). For now the token is a shared secret that both services read from `CASE_SERVICE_TOKEN`. The gateway uses it too, for the review app's reads (at the end).
- **Base URL:** the agent reads it from `CASE_SERVICE_URL`, for example `http://case:8000` inside Docker Compose. These calls go service to service, not through the gateway.
- **Not found:**
  - An unknown key in a path returns **404**, including an evidence ID that belongs to a different case.
  - A search with no hits returns **200 with `[]`**.
- **Dates:** ISO 8601 (`2026-11-14` or `2026-11-14T10:00:00`).
- **Same models as the contract.** Endpoints 7–10 return exactly the objects inside `CaseInput`, so build them from the `kyb_contracts` models (`Document`, `BankVerification`, `WebSnapshot`, `OwnerKYC`). They're a filtered view of the same tables.
- **Sensitive data stays in the database.** No response ever contains:
  - a full TIN, SSN or EIN (last 4 digits only, as in `CaseInput`)
  - a bank account number or routing number
  - a date of birth
  - raw OCR text or raw page text

  If a column holds one of these, leave it out of the response model.

## Endpoints

| # | Endpoint | Key | Worker tool | Evidence ID the worker assigns |
|---|---|---|---|---|
| 1 | `GET /registry/search` | state + name / officer / address | `search_registry` | R |
| 2 | `GET /registry/entities/{state}/{state_entity_id}` | `application.state_entity_id` | `get_registry_entity` | R |
| 3 | `GET /registry/entities/{state}/{state_entity_id}/filings` | same | `get_filing_history` | R |
| 4 | `GET /registry/dba` | state (+ county) + name | `search_dba` | R |
| 5 | `GET /commercial/search` | state + name / address | `search_commercial_db` | R |
| 6 | `GET /cases/{case_id}/tin-record` | `case_id` | `check_tin_match` | R |
| 7 | `GET /cases/{case_id}/owner-kyc/{kyc_id}` | `K1` | `get_kyc_result` | K (as given) |
| 8 | `GET /cases/{case_id}/documents/{doc_id}` | `D1` | `get_document` | D (as given) |
| 9 | `GET /cases/{case_id}/bank-verification/{bank_id}` | `B1` | `get_bank_verification` | B (as given) |
| 10 | `GET /cases/{case_id}/web-snapshots/{web_id}` | `W1` | `get_site_identity` | W (as given) |

Endpoints 1–7 serve the Records worker (official and third-party data); 8–10 serve the Evidence worker (merchant evidence). The examples use `case_1042` (Northline Bicycle Works) from `contracts/examples/case_input/`.

### Records worker: official and third-party data

#### 1. `GET /registry/search?state=IL&name=&officer=&address=`
Search the mocked Secretary of State registry. `state` is required, and at least one of `name`, `officer` or `address` is required. Keep the match **broad**: a case-insensitive `ILIKE` on each significant word, or `pg_trgm` similarity. The agent re-ranks the results with its own name library. Return at most 20 rows, each in the shape of endpoint 2.

#### 2. `GET /registry/entities/{state}/{state_entity_id}`
One registry entity, looked up directly by the ID the merchant entered (`application.state_entity_id`). Entity IDs are unique only within a state, so the state is part of the path.

```json
{
  "state": "IL",
  "state_entity_id": "IL-0458821",
  "legal_name": "NORTHLINE BICYCLE WORKS, L.L.C.",
  "former_names": ["HARBOR LIGHT CYCLES LLC"],
  "entity_type": "LLC",
  "entity_status": "active",
  "formation_date": "2023-05-30",
  "registered_address": "1450 W Fulton St, Chicago, IL 60607",
  "officers": [{"name": "Dana Park", "title": "Manager"}]
}
```
- `entity_type` uses `CaseInput`'s values: `LLC`, `CORPORATION` or `PARTNERSHIP`. Sole proprietors are never in the registry.
- `entity_status` is one of: `active`, `inactive`, `dissolved`, `suspended` or `pending`.
- `legal_name` is the name exactly as the state shows it, as in `checks.sos_name.matched_name`.

#### 3. `GET /registry/entities/{state}/{state_entity_id}/filings`
The filing history for one entity, oldest first. Return `[]` when it has none, since a brand-new LLC usually has none.

```json
[
  {"event_id": "IL-0458821-1", "event_type": "formation", "effective_date": "2023-05-30",
   "old_value": null, "new_value": "HARBOR LIGHT CYCLES LLC"},
  {"event_id": "IL-0458821-2", "event_type": "name_change", "effective_date": "2025-03-12",
   "old_value": "HARBOR LIGHT CYCLES LLC", "new_value": "NORTHLINE BICYCLE WORKS LLC"}
]
```
- `event_type` is one of: `formation`, `name_change`, `address_change`, `officer_change`, `dissolution` or `reinstatement`.

#### 4. `GET /registry/dba?state=IL&county=&name=`
Search the mocked DBA (assumed name) filings. `state` and `name` are required. `county` is optional: leave it out to search state-level filings and every county in the state. Use the same broad match on `dba`. The field names follow `CaseInput`'s `dba_certificate` fields, so a filing and a certificate can be compared field for field.

```json
[{
  "filing_id": "IL-AN-2024-0115",
  "state": "IL",
  "county": null,
  "dba": "NORTHLINE CYCLES",
  "registrant": "HARBOR LIGHT CYCLES LLC",
  "registrant_type": "entity",
  "registrant_state_entity_id": "IL-0458821",
  "filed": "2024-01-15",
  "expires": "2029-01-15",
  "status": "active"
}]
```
- `county` is `null` for a state-level filing.
- `registrant_type` is `individual` or `entity`. For an entity, fill in `registrant_state_entity_id` when it's known.
- `status` is `active`, `expired` or `withdrawn`.

#### 5. `GET /commercial/search?state=IL&name=&address=`
Search the mocked commercial database (LexisNexis / D&B style), for EC-4's `NO_COMMERCIAL_RECORD`. `state` is required, plus `name` or `address`. Broad match, at most 20 rows.

```json
[{
  "profile_id": "CDB-88213",
  "business_name": "NORTHLINE CYCLES",
  "address": "1450 W Fulton St, Chicago, IL 60607",
  "first_seen": "2024-02-01",
  "last_updated": "2026-09-30"
}]
```

#### 6. `GET /cases/{case_id}/tin-record`
The mocked IRS record behind the TIN the merchant submitted (`application.tin_type` + `tin_last4`). The case service returns the case's tin_record row, keyed by case_id (seeded from world.json). It doesn't look up by the full number, and nothing it returns contains it. The agent computes the TIN-match result itself, from the name it's testing and `irs_name`.

```json
{"tin_type": "EIN", "tin_last4": "4567", "irs_status": "issued", "irs_name": "HARBOR LIGHT CYCLES LLC"}
```
- `irs_status` is `issued`, `not_issued` (a brand-new EIN, `checks.tin` = Not Issued) or `not_found` (no such number, `checks.tin` = Not Found). `irs_name` is `null` unless it's `issued`.
- For a sole proprietor filing under an SSN, `tin_type` is `SSN` and `irs_name` is the owner's name.
- Return **404** when the case has no submitted TIN (`tin_type` is null).
- **Never return the full number.**

#### 7. `GET /cases/{case_id}/owner-kyc/{kyc_id}`
One owner's KYC result: `CaseInput.owner_kyc[]`, unchanged.

```json
{"id": "K1", "owner_ref": "owner:P1", "result": "pass"}
```
- `result` is `pass`, `fail` or `pending`, as in `case_input.py`.

### Evidence worker: merchant evidence

#### 8. `GET /cases/{case_id}/documents/{doc_id}`
One uploaded document with its fields already extracted: `CaseInput.documents[]`, unchanged. The keys inside `extracted` are listed per `type` in `DOCUMENT_FIELDS` (`case_input.py`); any of them may be missing.

```json
{
  "id": "D1",
  "type": "irs_cp575",
  "extracted": {"name": "HARBOR LIGHT CYCLES LLC", "ein_last4": "4567", "date": "2023-06-08"},
  "confidence": 0.96
}
```
- `type` is one of: `irs_cp575`, `irs_147c`, `articles`, `articles_of_amendment`, `dba_certificate`, `w9`, `good_standing` or `business_license`.
- No OCR text and no file.

#### 9. `GET /cases/{case_id}/bank-verification/{bank_id}`
The bank verification result: `CaseInput.bank_verification`, unchanged. Bank statements and voided checks aren't documents; the bank verification service reduces them to this.

```json
{"id": "B1", "account_holder": "NORTHLINE BICYCLE WORKS LLC DBA NORTHLINE CYCLES", "status": "verified", "verified_at": "2026-11-10"}
```
- `status` is `verified`, `unverified`, `pending` or `failed`.

#### 10. `GET /cases/{case_id}/web-snapshots/{web_id}`
One saved website snapshot: `CaseInput.web_snapshot[]`, unchanged.

```json
{
  "id": "W1",
  "url": "https://northlinecycles.example/terms",
  "captured_at": "2026-11-10",
  "extracted": {"legal_name": "Northline Bicycle Works LLC", "dba": "Northline Cycles"}
}
```
- The keys inside `extracted` are limited to `WEB_FIELDS`: `legal_name`, `dba`, `address`. Any may be missing.
- No page text. Some golden cases put instruction-like text aimed at AI into these values to test prompt injection; store them as they are.

### Step 11: saving the analyst's decision

#### `POST /cases/{case_id}/decisions`
The agent service calls this once the analyst has decided. The body is the `DecisionRecord` (`decision.py`) and, optionally, the case status the sender expects:

```json
{
  "decision_record": {
    "review_id": "rv_7f3a", "case_id": "case_1042", "decision": "approve",
    "note": "Name change confirmed by the SoS amendment.", "analyst_id": "u_analyst_02",
    "agent_recommendation": "recommend_clear", "overridden": false,
    "decided_at": "2026-11-17T16:05:00-06:00"
  },
  "case_status": "approved"
}
```
- In one transaction, the case service saves the record, sets the case status to `STATUS_AFTER[decision]`, and writes an audit log entry: who (`analyst_id`), from which status to which, and when. Nothing else changes a case's status.
- **201** with the same body, `case_status` filled in. **200** if this review's decision was already saved with the same content, so a retry is safe.
- **404** for an unknown case.
- **409** if the review already has a different decision, or the case can't take a decision now. Only `pending_review` and `escalated` cases can: an approved or denied case is done, and a `docs_requested` case waits for the merchant's new evidence.
- **422** if `decision_record.case_id` isn't the path's case, `case_status` isn't the status the decision leads to, or the record is invalid (for example, `overridden` doesn't match `AGREES_WITH`).

### For the review app

The review app calls these through the gateway, which adds the service token ([auth.md](auth.md)):

- `GET /cases?status=pending_review`: the case queue, oldest first. Each row has `case_id`, `status`, `legal_name`, `dba`, `state`, `flag_reasons`, `created_at` and `updated_at`. Without `status` it lists every case; an unknown status is 422.
- `GET /cases/{case_id}/decisions`: the case's `DecisionRecord`s, oldest first. 404 for an unknown case.
- `GET /metrics/override-rate`: how often analysts decide against the agent's recommendation (BEAC-20). It returns `decisions`, `overridden` and `rate` (`null` before the first decision), and the same three per recommendation in `by_recommendation`.

## Seed data

Each golden case is a folder `data/golden/<ec>/<case_id>/`. Each file has a model in `contracts/kyb_contracts/`, and `scripts/validate_golden.py` checks that a case's files agree with each other (see `data/golden/README.md`):

| File | Holds | Model | Loaded into |
|---|---|---|---|
| `case.json` | One `CaseInput` | `CaseInput` (`case_input.py`) | The case tables, served by `GET /cases/{case_id}` and endpoints 7–10 |
| `world.json` | `case_id` and the reference data: `registry` (endpoint 2 shape, with `filings`), `dba_filings`, `commercial` (endpoint 5 shape, plus the `state` it filters on), `tin_record` (endpoint 6 shape) | `World` (`world.py`) | The mocked registry, DBA, commercial and IRS tables (endpoints 1–6) |

It would help if your seed script loaded these files, so the agent's offline tests (a fake client that reads the same files) and the real database use the same data.

## Changes from the first version (Nov 18 morning)

| Before | Now | Why |
|---|---|---|
| `GET /tin-records/{tin_ref}` | `GET /cases/{case_id}/tin-record` | `CaseInput` has no TIN reference; the case has one submitted TIN, so `case_id` is the key |
| `GET /kyc/{kyc_ref}`, status `verified`/`partial`/`unverified` | `GET /cases/{case_id}/owner-kyc/{kyc_id}`, result `pass`/`fail`/`pending` | Use `CaseInput`'s K IDs and values |
| Document `doc_id`/`doc_type`, types like `ein_letter_147c`, `voided_check` | `CaseInput.Document`: `id`/`type`, `irs_147c` etc.; bank docs moved to endpoint 9 | One document format, defined in `case_input.py` |
| Web snapshot with `text`, `footer_legal_name`, `dba_name` | `CaseInput.WebSnapshot` with `extracted` | Raw text stays in case_db |
| `registry_id`; `GET /registry/{registry_id}/filings` | `state_entity_id`, scoped by state; new direct lookup (endpoint 2) | Matches `application.state_entity_id` |
| DBA fields `dba_name`, `owner_name`, `filed_at` | `dba`, `registrant`, `filed`, `expires` | Same names as the `dba_certificate` fields |
| — | Endpoint 5, commercial database search | For `NO_COMMERCIAL_RECORD` (EC-4) |

## What I need back from you

1. Changes to any path or field name.
2. Whether the seed layout (`case.json` and `world.json`) works for your seed script.
3. Roughly when the endpoints will be running in Docker Compose. Until then, the agent tests against a fake client that reads the golden files.
