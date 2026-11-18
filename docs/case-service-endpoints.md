# Case service: read endpoints the agent workers need

**From:** the sub-agents (Records and Evidence workers)
**To:** the case-service owner
**Status:** proposal, Nov 18. Please reply with changes; the agent-side client gets built against this on Nov 19.

The Records and Evidence workers read all their data from the case service. Each tool makes one `GET` call. These 7 endpoints are read-only, and only the agent service calls them.

## Common rules

- **Auth:** `Authorization: Bearer <service JWT>` with role `agent_service`. That role is read-only and gets only these endpoints.
- **Base URL:** the agent reads it from `CASE_SERVICE_URL`, for example `http://case:8000` inside Docker Compose. These calls go service to service, not through the gateway.
- **Not found:**
  - An unknown id in a path (`/kyc/{kyc_ref}`) returns **404**.
  - A search with no hits returns **200 with `[]`**.
- **Dates:** ISO 8601 (`2026-11-14` or `2026-11-14T10:00:00`).
- **Sensitive data stays in the database.** No response ever contains:
  - a full TIN, SSN or EIN (`last4` is fine)
  - a bank account number or routing number
  - a date of birth

  If a column holds one of these, leave it out of the response model.

## Endpoints

### 1. `GET /registry/search?state=NY&name=&owner=&address=`
Search the mocked Secretary of State registry. `state` is required, and at least one of `name`, `owner` or `address` is required. Keep the match **broad**: a case-insensitive `ILIKE` on each significant word, or `pg_trgm` similarity. The agent re-ranks the results with its own name library. Return at most 20 rows.

```json
[{
  "registry_id": "NY-5521873",
  "state": "NY",
  "legal_name": "Hemlock Holdings LLC",
  "entity_type": "llc",
  "entity_status": "active",
  "formation_date": "2026-11-12",
  "registered_address": "12 Main St, Great Neck, NY 11021",
  "officers": [{"name": "Mark Hemlock", "title": "member"}]
}]
```
- `entity_status` is one of: `active`, `inactive`, `dissolved`, `suspended` or `pending`.

### 2. `GET /registry/dba?state=NY&county=Nassau&name=`
Search the mocked county DBA (assumed name / FBN) filings. Use the same broad match on `dba_name`.

```json
[{
  "filing_id": "NASSAU-2026-0412",
  "state": "NY",
  "county": "Nassau",
  "dba_name": "Sunshine Cafe",
  "owner_type": "individual",
  "owner_name": "Mark Hemlock",
  "legal_name": null,
  "registry_id": null,
  "filed_at": "2026-10-02",
  "status": "active"
}]
```
- `owner_type` is `individual` or `entity`.
- When the owner is an entity, fill in `legal_name` and `registry_id`.

### 3. `GET /registry/{registry_id}/filings`
The filing history for one registry entity, oldest first. Return `[]` when it has none, since a brand-new LLC usually has none.

```json
[{
  "event_id": "ev_1",
  "registry_id": "NY-5521873",
  "event_type": "formation",
  "effective_date": "2026-11-12",
  "old_value": null,
  "new_value": "Hemlock Holdings LLC"
}]
```
- `event_type` is one of: `formation`, `name_change`, `address_change`, `officer_change`, `dissolution` or `reinstatement`.

### 4. `GET /tin-records/{tin_ref}`
The mocked IRS record behind a TIN reference. The agent computes the TIN-match code itself from `irs_name`.

```json
{"tin_ref": "tin_ref_8841", "tin_type": "SSN", "irs_name": "Mark Hemlock", "issued": true, "last4": "4417"}
```
- `tin_type` is `SSN` or `EIN`.
- `issued: false` simulates "TIN not issued" (IRS code 2).
- **Never return the TIN digits.**

### 5. `GET /kyc/{kyc_ref}`
The owner's KYC result, with the result only.

```json
{"kyc_ref": "kyc_77", "owner_ref": "o1", "status": "verified", "checked_at": "2026-11-15T08:30:00", "summary": "name and address verified"}
```
- `status` is one of: `verified`, `partial` or `unverified`.
- ⚠ The README's `OwnerKYC.result` uses `pass` / `fail` / `pending`. We need to pick one set of values. I'd suggest the three above, because `partial` matters for EC-4.

### 6. `GET /cases/{case_id}/documents/{doc_id}`
One uploaded document with its fields already extracted. This is the README's `Document`, plus `uploaded_at`. The keys inside `extracted` depend on `doc_type`:

| `doc_type` | `extracted` keys |
|---|---|
| `ein_letter_cp575`, `ein_letter_147c` | `legal_name`, `ein_masked` (e.g. `**-***4417`), `irs_name_control`, `document_issue_date`, `address` |
| `dba_certificate` | `dba_name`, `owner_name`, `legal_name`, `county`, `state`, `filing_date` |
| `business_license` | `license_holder`, `license_status`, `operating_address`, `document_issue_date`, `issuing_authority` |
| `bank_statement`, `voided_check` | `bank_account_holder` **only** |

```json
{
  "doc_id": "d1",
  "case_id": "case_0012",
  "doc_type": "ein_letter_147c",
  "uploaded_at": "2026-11-14T10:00:00",
  "confidence": 0.95,
  "extracted": {
    "legal_name": "Mark Hemlock",
    "ein_masked": "**-***4417",
    "irs_name_control": "HEML",
    "document_issue_date": "2026-03-02",
    "address": "12 Main St, Great Neck, NY 11021"
  }
}
```
- `confidence` is a float from 0 to 1, as in the README.
- Return **404** if the document doesn't belong to `case_id`.

### 7. `GET /cases/{case_id}/web-snapshots?url=`
The saved snapshot of the merchant's website. This is the README's `WebSnapshot`, plus three fields parsed from it.

```json
{
  "id": "ws_1",
  "url": "https://sunshinecafe.example",
  "captured_at": "2026-11-14",
  "text": "Sunshine Cafe ... (c) 2026 Mark Hemlock dba Sunshine Cafe ...",
  "footer_legal_name": "Mark Hemlock dba Sunshine Cafe",
  "dba_name": "Sunshine Cafe",
  "address": "12 Main St, Great Neck, NY 11021"
}
```
- The parsed fields may be `null`.
- `text` may contain hidden instructions in some golden cases, to test prompt injection. Store it as it is.

## Seed data

I'm writing 6 fixture cases on Nov 19 as `data/golden/<ec>/<case_id>/world.json`, with one key per endpoint above (`registry`, `county_dba`, `filing_history`, `tin_records`, `kyc`, `documents`, `web_snapshots`). It would help if your seed script loaded these files, so the agent's offline tests and the real database use the same data.

## What I need back from you
1. Changes to any path or field name.
2. The decision on the KYC status values (endpoint 5).
3. Roughly when the endpoints will be running in Docker Compose. Until then, I'll test against a fake client that reads `world.json`.
