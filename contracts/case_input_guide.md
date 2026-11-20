# case_input.py Guide: Every Part of CaseInput and the KYB Knowledge Behind It

> Goes through `case_input.py` from top to bottom: what each class and field is, how the project uses it, and the KYB domain knowledge you need to read it. Python and Pydantic basics aren't covered here.
> All companies, people and numbers in the examples are fictional. The sample cases are in `contracts/examples/case_input/`.

## Contents

1. Overview
2. The opening docstring: what CaseInput is, and four rules
3. `Contract`: the base for every model
4. Enums and fixed values
5. `Owner` and `Application`: what the merchant entered
6. `Checks`: what the existing system already ran
7. Merchant evidence: `Document`, `BankVerification`, `WebSnapshot`, `OwnerKYC`
8. `CaseInput`: the top level
9. An example for each FlagReason
10. Term index

---

## 1. Overview

KYB (Know Your Business) means verifying a business: does it exist, do its name and tax ID line up, who owns it. Our project handles the exception cases that get flagged during KYB review.

A US business's identity is spread across several systems that don't sync with each other: the state records who the company is, the IRS records whom each tax ID was issued to, and the DBA registry records who owns each trade name. When a merchant applies to Affirm, most of what it enters is copied from documents these systems gave it. The existing system compares the application with each system, and every mismatch becomes a flag.

All three of our ECs come down to "the application doesn't match some system":

- **EC-1 Names:** the name doesn't match across the application, the state record and the DBA registry.
- **EC-2 TIN:** the combination of tax ID and name doesn't match at the IRS.
- **EC-4 New or unregistered businesses:** the records don't have the business yet (too new), or never will (sole proprietors).

```
Merchant: forms the company → gets an EIN → registers a DBA → opens a bank account → builds a website
   │ each step leaves a record in some system, and usually a document
   ▼
State registry · IRS · DBA registry · bank · commercial databases (each keeps its own records, not synced)
   │
   ▼
Merchant applies to Affirm: fills in the application, uploads documents
   │
   ▼
Existing system compares the application with each system → each mismatch becomes a flag
   │
   ▼
Our agent finds out why and writes a memo → the analyst decides
```

### 1.1 Workflow steps mentioned in this doc

The "In the project" columns refer to steps in the workflow. Here they are in order; the workflow doc `affirm-workflow-handoffs.md` has the full details.

| Step | Done by | What it does |
|---|---|---|
| Case JSON | Existing system | Hands the case (one `CaseInput`) to the case service |
| Intake | Code | Prepares the case first: normalizes names, computes name controls (see 6.4), marks `NAME_SIMILAR_MATCH` resolved when the names differ only in formatting, and sets aside out-of-scope flags |
| Lead | LLM | Reads the case and gives both workers questions to investigate; afterwards decides whether to follow up (at most two rounds), then writes the memo |
| Records worker | LLM with tools | Looks up official records and third-party data: the state registry, the DBA registry, IRS TIN matching, commercial databases and the owners' KYC results |
| Evidence worker | LLM with tools | Fetches the merchant-side evidence in the case by ID: the extracted fields of uploaded documents, bank verification and website snapshots |
| Reconcile | Code | Puts what the two workers found side by side and finds what still doesn't match |
| Checks | Code | After the memo is written, checks it against a few hard rules; if it fails, sends it back to the Lead to fix |
| Analyst decision | Analyst | Reads the memo and makes the final decision: approve, request documents, escalate (hand the case to someone more senior) or deny |
| Decision saved | Code (case service) | Saves the analyst's decision, updates the case's `status` to match, and writes the audit log, all in one transaction |

The memo is the agent's review report for the analyst: for each flag, the conclusion, the cause, the evidence IDs and a recommendation.

---

## 2. The opening docstring: what CaseInput is, and four rules

In the code: the docstring at the top of the file.

### 2.1 Where CaseInput comes from and who uses it

A `CaseInput` is one merchant's application (a merchant is a business that wants to take payments through Affirm), plus the checks the existing system already ran and the evidence the merchant provided. It's the first step of the workflow.

The existing system is Affirm's current automated review: it takes applications, calls verification services and raises flags. Our project has no real existing system; sample case files and a script that loads them into the database stand in for it.

```
Existing system (in our project: sample files and a script)
   │ stores the case
   ▼
Case service (data in case_db)
   │ GET /cases/{case_id}
   ▼
Agent service: Intake → Lead → Records worker, Evidence worker → ...
```

- **Who uses it:** Intake, the graph (the Lead steps), the Records worker and the Evidence worker.
- **Changing it:** that's why any change to `case_input.py` goes through a PR reviewed by the owners of those parts.

### 2.2 Rule 1: structure only, no business rules

The contract checks only types, formats and IDs, not business rules such as "an LLC must have a state entity ID".

That's because missing fields and odd-looking data are exactly what the agent has to handle. A sole proprietor, for example, has no state entity ID and may have no EIN (`case_2001`). If the contract rejected such a case, the agent would never see it. That's why many fields can be empty (see section 5).

Likewise, the contract doesn't check whether the data agrees with itself (for example, whether `checks` and `flag_reasons` line up); see 6.6.

### 2.3 Rule 2: full tax IDs never go into CaseInput

An SSN (Social Security Number) is a person's number: 9 digits, written XXX-XX-XXXX. Sole proprietors often file taxes under it. An EIN is a business's number: 9 digits, written XX-XXXXXXX.

- The application carries only the TIN's type and last 4 digits (`tin_type`, `tin_last4`; see 5.3). Document fields do the same (`ein_last4`, see 4.5).
- In CaseInput, owners appear only as reference IDs such as `owner:P1` (see 5.2).
- Our system never stores the full SSN or EIN. The existing system already used them, for TIN matching and KYC, before handing the case over; the results are in `checks.tin` and `owner_kyc`.
- To test another name, the Records worker calls `tin_match` with only that name. The tool gets the case's IRS record by `case_id` and compares the names in code, so no full number is needed.
- How full numbers are kept out: see `TIN_PATTERN` in 4.6 and the integrity check in section 8.

### 2.4 Rule 3: everything arrives parsed, and merchant claims stay on the Evidence side

The existing system has already parsed everything: the application, the vendor checks, the fields extracted from each document, the bank verification, the website fields and the KYC results. The workers parse nothing; their tools fetch these stored records by evidence ID (see 2.5).

Raw merchant text (a document's full OCR text, a website's page text) stays in case_db and isn't part of CaseInput. The merchant can write anything there, including hidden instructions aimed at AI, so it never reaches the model.

The extracted values are still the merchant's claims. Only the Evidence worker reads documents, bank verification and web snapshots, and it never passes them to the Lead as facts; every other step sees only the findings it reports. Owner KYC comes from a third-party vendor, so the Records worker reads it.

### 2.5 Rule 4: evidence IDs are fixed when the case arrives

| ID | What it is | Who assigns it | Where it lives |
|---|---|---|---|
| R | Results from official systems and commercial databases | The Records worker, as it looks them up; unique across the whole review (the Lead passes `record_id_start`) | `WorkerResult.sources[].source_id` (see `worker_io.py`) |
| D | Documents the merchant uploaded | The existing system | `CaseInput.documents` |
| B | Bank verification result | The existing system | `CaseInput.bank_verification` |
| W | Website snapshot | The existing system | `CaseInput.web_snapshot` |
| K | The owner's KYC result | The existing system | `CaseInput.owner_kyc` |

D, B, W and K IDs are assigned when the case arrives and never change. The memo cites evidence by ID only, and Checks looks each ID up again to build `evidence_index`. If an ID changed, the citation would point to the wrong thing.

In the workers' output, a source's `source_id` is its evidence ID. The Records worker may cite only R and K; the Evidence worker only D, B and W. `worker_io.py` rejects anything else.

The source matters too: Checks has a hard rule that to mark a flag resolved, the memo must cite both an official or third-party record (R, or K for owner KYC) and merchant-side evidence (D, B or W).

---

## 3. `Contract`: the base for every model

Every model inherits from `Contract`, which sets `extra="forbid"`: a field the model doesn't define is an error.

Why not drop it quietly? Suppose someone writes `matched_name` as `matchedName`. If unknown fields were ignored, it would be dropped and `matched_name` would be empty. Under the rule in 6.2, a case whose name didn't match would then look like "nothing found". Raising an error catches this kind of problem the moment the data comes in.

`extracted` is a plain dictionary, and `extra="forbid"` doesn't cover its keys, so they're checked separately against `DOCUMENT_FIELDS` (see 4.5).

---

## 4. Enums and fixed values

### 4.1 `CaseStatus`: where the case stands

| Value | Meaning | When the case gets this status | What happens next |
|---|---|---|---|
| `pending_review` | Waiting for the agent and the analyst to review | The case arrives from the existing system with this status | The agent reviews, then the analyst decides |
| `approved` | The analyst approved | The analyst chose approve | Done |
| `docs_requested` | Waiting for the merchant to send more evidence | The analyst chose request documents | Once the merchant sends it, the case goes back to `pending_review`, or a new case is opened and this one closed (to decide) |
| `escalated` | Handed to a senior analyst | The analyst chose escalate | The senior analyst decides: approve, deny or request documents |
| `denied` | The analyst denied | The analyst chose deny | Done |

```
                ┌─ approve ───────────▶ approved (done)
                ├─ deny ──────────────▶ denied (done)
pending_review ─┼─ request documents ─▶ docs_requested ── merchant sends more ──▶ pending_review (or a new case, to decide)
                └─ escalate ──────────▶ escalated ── senior analyst decides ──▶ approved / denied / docs_requested
```

- **Code and JSON spell it differently:** code uses names like `CaseStatus.PENDING_REVIEW`; JSON uses the value `pending_review`.
- **Only Decision saved changes the status (see 1.1).** It turns the decision into a status with `STATUS_AFTER` in `decision.py`, in the same transaction as saving the decision and writing the audit log. The agent never changes a case's status; it only writes the memo and a recommendation.
- **Don't confuse it with the review status.** Each review has its own status in the agent service (running, awaiting_decision, done, failed), which is what the frontend polls at `GET /reviews/{review_id}`. `CaseStatus` is the status of the case itself, stored in case_db.
- **To decide:** after the merchant sends more documents, does the case go back to `pending_review`, or is a new case opened?

### 4.2 `FlagReason`: why the case was flagged

The reasons the existing system flagged the case, one value per problem, grouped by EC.

| Value | EC | Meaning | Mainly comes from |
|---|---|---|---|
| `NAME_SIMILAR_MATCH` | EC-1 | The applied name is close to the state record, but not identical | `checks.sos_name` is Similar Match |
| `NAME_UNVERIFIED` | EC-1 | The company was found by entity ID, but the applied name doesn't match | `checks.sos_name` is Unverified, with a `matched_name` |
| `DBA_UNVERIFIED` | EC-1 | The DBA can't be tied to this business | `checks.dba` is Unverified |
| `TIN_MISMATCH` | EC-2 | The TIN and name don't match at the IRS | `checks.tin` is Mismatch |
| `TIN_NOT_FOUND` | EC-2 | The IRS has no record of the TIN | `checks.tin` is Not Found |
| `TIN_NOT_ISSUED` | EC-2 | The number hasn't been issued yet; common for brand-new EINs | `checks.tin` is Not Issued |
| `ENTITY_NOT_FOUND` | EC-4 | No state registration found | `checks.sos_name` is Unverified, with a null `matched_name` |
| `NO_COMMERCIAL_RECORD` | EC-4 | The company isn't in the commercial databases | `checks.commercial_db` is Not Found |
| `PRODUCT_CATEGORY_REVIEW` | Out of scope | What the merchant sells falls in a category that needs a closer human look | None of the four checks |
| `OFAC_POTENTIAL_MATCH` | Out of scope | The name may be on the US Treasury's sanctions list (the SDN list) | None of the four checks |

The note above each group in the code gives the scope of each EC:

| EC | What the code says it covers | Systems involved | Key terms | Evidence that usually explains it |
|---|---|---|---|---|
| EC-1 Names | Legal name vs DBA / suffix & punctuation variants | State registry, DBA registry | legal name, DBA, former name, entity suffix, filing history | Formation documents, amendments, DBA certificates, bank account name, website |
| EC-2 TIN | EIN/TIN mismatch, including IRS name-control issues | IRS | TIN, EIN, SSN, TIN matching, name control | CP 575, 147C, W-9, amendments |
| EC-4 New or unregistered | Newly formed businesses and sole proprietors missing from databases | State registry, commercial databases, KYC; for sole proprietors, also the DBA registry and the IRS | sole proprietorship, formation date, foreign qualification, commercial database, KYC | Formation documents, CP 575, DBA certificates, W-9, KYC results, bank account name |

This is how these flags usually come about:

| What happened | State registry | IRS | DBA registry | Bank | Commercial databases | Flags it can cause |
|---|---|---|---|---|---|---|
| Company changed its name | Updated once the amendment is filed | Updated only if the company reports it, and even then it takes a while | Not updated automatically | Updated if notified | Catch up slowly | `NAME_UNVERIFIED`, `DBA_UNVERIFIED`, `TIN_MISMATCH` |
| Company just formed | Record exists right away | The EIN can be used to open a bank account right away, but it takes a while to pass TIN matching | DBA may not be registered yet | Can open an account | Often months before it appears | `TIN_NOT_ISSUED`, `NO_COMMERCIAL_RECORD` |
| Sole proprietor | No record, because there's nothing to register | With an SSN, the IRS has the owner's personal name; comparing it with the shop name won't match | Often registered at the county level | Account name is often "OWNER NAME DBA SHOP NAME" | May be missing | `ENTITY_NOT_FOUND`, `TIN_MISMATCH`, `DBA_UNVERIFIED` |
| Formed in another state | Record in the state of formation; the operating state needs its own registration | Not affected | — | — | — | `ENTITY_NOT_FOUND` or `NAME_UNVERIFIED` |
| Application filled in wrong | — | — | — | — | — | Depends on which field is wrong |

A few notes:

- **The two out-of-scope flags:** Intake passes them to the analyst as is, and the agent doesn't handle them. They keep their own names because the analyst needs to know what the problem is; OFAC especially needs separate handling, since US persons are generally prohibited from dealing with the people and entities on the list.
- **Flags not in the enum:** the whole case is rejected, because `flag_reasons` accepts only these values. In the POC we write all the data ourselves, so this is fine for now.
- `flag_reasons` must have at least one flag; see section 8. See section 9 for a full example of each flag.

### 4.3 `EntityType`: entity types

| Value | What it is | What it means | Registers with the state? | Tax ID |
|---|---|---|---|---|
| `LLC` | Limited Liability Company | The most common form for small businesses. An LLC with one member is a single-member LLC and is also written `LLC`; for taxes it can be treated as the owner, and the tax term is disregarded entity | Yes | Usually an EIN; a single-member LLC may use the owner's SSN |
| `CORPORATION` | Corporation | The name usually ends in Inc. or Corp. | Yes | EIN |
| `PARTNERSHIP` | Partnership | Two or more people doing business together | Depends on the type; some do | EIN |
| `SOLE_PROPRIETORSHIP` | Sole proprietorship | One person doing business directly; legally, the business and the person are the same | No | The owner's SSN, or an EIN if they get one |

LPs and LLPs can only go under `PARTNERSHIP`; other types, such as nonprofits, can't be represented yet.

### 4.4 `DocumentType`: the documents a merchant can upload

Apart from the W-9, which the merchant fills in, every document is issued by some authority and shows that authority's records as of the issue date. So a document proves what was true on the day it was issued, not what is true now.

A real-world example: Affirm's merchant help pages say that a merchant already on Affirm that changes its EIN must send an IRS EIN document (such as a 147C), and one that changes its name must send the name change filing approved by the state. Those are `irs_147c` and `articles_of_amendment` below.

| Value | Document | Issued by | What it is | What it proves | Watch out for | ECs |
|---|---|---|---|---|---|---|
| `irs_cp575` | CP 575 (EIN assignment notice) | IRS | The confirmation letter the IRS sends when it issues an EIN | Which name the EIN was originally issued to | Shows the name at the time; it isn't updated after a name change. The original is issued only once and isn't replaced if lost: the merchant can call the IRS to request a 147C, and eligible businesses can now also download a digital copy from their IRS Business Tax Account | EC-2, EC-4 |
| `irs_147c` | Letter 147C (EIN verification letter) | IRS | An EIN verification letter the merchant requests from the IRS | Which name the IRS has on file now | The strongest document for telling whether the IRS still has the old name | EC-2 |
| `articles` | Articles of Organization / Incorporation (formation documents) | The state | The document filed with the state to form the company. LLCs file Articles of Organization and corporations file Articles of Incorporation; some states call it a Certificate of Formation | The name and date at formation | Only reflects the time of formation; after a name change, look at the amendment | EC-1, EC-4 |
| `articles_of_amendment` | Articles of Amendment | The state | The document filed with the state for changes such as a new name; also called a Certificate of Amendment | That the old and new names are the same company | The most direct proof of a name change | EC-1, EC-2 |
| `dba_certificate` | DBA certificate | DBA registry (state or county) | The certificate received after registering a DBA; also called a Certificate of Assumed Name | Whose name the DBA is registered under | The registrant may be an old name; the DBA may have expired | EC-1, EC-4 (sole proprietors) |
| `w9` | W-9 (tax ID form) | Filled in by the merchant | The form a merchant gives a payer, with its name and tax ID | The name and tax ID the merchant claims | Not official proof. Sole proprietors put their own name on line 1 and the shop name on line 2 | EC-2, EC-4 (sole proprietors) |
| `good_standing` | Certificate of Good Standing | The state | Proves the company is in good standing: it has filed its annual reports, paid its fees and is currently valid. Some states call it a Certificate of Existence. Affirm's blog says it checks this | The company was valid on the issue date | Goes stale; check the issue date | EC-1, EC-4 |

### 4.5 `DOCUMENT_FIELDS`: the fields extracted from each document type

The fields the existing system extracts from each document type. The rule: fields may be missing (extraction can fail), but no field outside the list is allowed. The check that enforces this is `known_fields_only` in 7.1.

| `type` | Extracted fields | Notes |
|---|---|---|
| `irs_cp575` | `name`, `ein_last4`, `name_control`, `address`, `date` | The name, the EIN's last 4 digits, the IRS name control, the address and the date on the letter |
| `irs_147c` | `name`, `ein_last4`, `name_control`, `address`, `date` | Same as above; here `name` is the name the IRS has on file now |
| `articles` | `legal_name`, `state`, `state_entity_id`, `formation_date` | Legal name at formation, state, entity ID, formation date |
| `articles_of_amendment` | `old_name`, `new_name`, `state_entity_id`, `date` | Names before and after the change, entity ID, date of the change |
| `dba_certificate` | `dba`, `registrant`, `jurisdiction`, `filed`, `expires` | DBA, registrant, where it was registered (state or county, such as `Kings County, NY`), filing date, expiration date |
| `w9` | `legal_name`, `business_name`, `tin_type`, `tin_last4` | The name on line 1, the business name on line 2, the TIN type (SSN or EIN) and the TIN's last 4 digits. Only the last 4 digits reach us |
| `good_standing` | `legal_name`, `status`, `date` | Legal name, status, issue date |
| `business_license` | `holder`, `status`, `address`, `issued`, `authority` | License holder, status (such as active or pending), operating address, issue date, issuing city, county or state |

Website snapshots have their own list, `WEB_FIELDS`: `legal_name`, `dba`, `address` (see 7.3).

All values are strings, dates included, for example `"2023-06-08"`.

### 4.6 `TIN_PATTERN`: the regex that spots full tax IDs

`TIN_PATTERN` matches a full SSN (`XXX-XX-XXXX`), a full EIN (`XX-XXXXXXX`) or 9 digits written together. The integrity check in section 8 turns the whole case into JSON text and scans it with this pattern; any match rejects the case. It doesn't matter which field the number is in: extracted fields, names and addresses all count. `state_entity_id` is the one field skipped, because some states issue 9-digit entity IDs. (`worker_io.py` imports the same pattern for the workers' context.)

- **Test data convention:** fake EINs always start with `00` and fake SSNs with `000`. Real numbers never start this way, so a fake is obvious at a glance. But fake numbers are rejected by this check too, so in the golden cases full ones live only in `secrets.json`, never in CaseInput.
- **Limits:** another number that happens to have one of these formats, such as a 9-digit ZIP+4 written without the dash, is rejected by mistake.
- `SSN_PATTERN` (dashed SSNs only) is kept for existing imports; `TIN_PATTERN` covers it.

---

## 5. `Owner` and `Application`: what the merchant entered

### 5.1 Where the application's information comes from

Everything in `application` is the merchant's claim, not a verified fact; the later steps exist to verify it. Merchants mostly fill in the application by copying from documents they have, and the existing system then compares each field with the matching system.

| Application field | What the merchant copies it from | What the existing system compares it with | Where the result goes | ECs |
|---|---|---|---|---|
| `legal_name` | Formation or amendment documents; for a sole proprietor, their own name | The state registry; TIN matching usually uses this name too | `checks.sos_name`, `checks.tin` | EC-1, EC-2 |
| `dba` | DBA certificate | The DBA registry | `checks.dba` | EC-1 |
| `address` | The merchant's own records | Registry addresses, license and website addresses | No separate check | EC-1, EC-4 |
| `tin_type`, `tin_last4` | CP 575, 147C or W-9; for a sole proprietor, the owner's SSN | The IRS (TIN and name together) | `checks.tin` | EC-2 |
| `state`, `state_entity_id` | Formation documents | The state registry, to find the right company | `checks.sos_name` | EC-1, EC-4 |
| `entity_type` | Formation documents; sole proprietors don't have any | The state registry | No separate check | EC-4 |
| `formation_date` | Formation documents | The state registry | No separate check; used to tell whether the company is new | EC-4 |
| `owners` | Filled in by the merchant | KYC services | `owner_kyc` | EC-4 |

When a sole proprietor has no EIN, TIN matching uses the owner's SSN: `tin_type` is `SSN` and `tin_last4` holds its last 4 digits. The full number stays with the existing system (see 2.3).

### 5.2 `Owner`: the people behind the business

| Field | Meaning | Format |
|---|---|---|
| `ref` | Owner reference: stands in for the SSN everywhere in the system | `owner:P` plus digits, such as `owner:P1` |
| `name` | The owner's name | Text |
| `role` | The person's role in the company, shown only to the analyst | Text, such as `Owner`, `Manager` or `Member` |

- **Owner / Manager / Member:** the people who own or run the business. In an LLC, the owners are called members and the people who run it are managers.
- **Beneficial owner:** the person who actually owns or controls the business, and the person KYC verifies (see 7.4).

### 5.3 `Application`: the application form

| Field | Meaning | Format | Can it be empty? | Notes |
|---|---|---|---|---|
| `legal_name` | Legal name: the official name registered with the state; for a sole proprietor, their full personal name | Text | No | The IRS keeps a name of its own: the one the EIN was issued under, or a later one reported to the IRS. When the two differ, you get EC-2 |
| `dba` | DBA (doing business as): the name a business trades under; also called an assumed name, fictitious business name or trade name | Text | Yes | The existing system runs `checks.dba` only when a DBA is filled in |
| `address` | The business address the merchant entered | Text | No | The Records worker uses it to search the registry; the Evidence worker compares it with license and website addresses |
| `tin_type` | Which tax ID was submitted: `EIN` (Employer Identification Number, a business's federal tax ID; a name change doesn't need a new one) or `SSN` (a sole proprietor filing under their own number) | `EIN` or `SSN` | Yes | Empty only when no TIN was submitted; then `tin_last4` is empty too |
| `tin_last4` | The submitted TIN's last 4 digits | 4 digits | Yes | Given together with `tin_type`. The full number stays with the existing system (see 2.3) |
| `state` | The state where the business is registered or operates | Two capital letters, such as `IL` | No | Decides which state's records to search |
| `state_entity_id` | State entity ID: the number the state gives each company | Varies by state | Yes | Sole proprietors usually have none |
| `entity_type` | Entity type | See 4.3 | No | — |
| `formation_date` | Formation date: the date the company was formed with the state | Date | Yes | EC-4 uses it to tell whether the company is new; usually empty for sole proprietors |
| `owners` | List of owners | See 5.2 | At least one | — |

TIN (Taxpayer Identification Number) is the umbrella term for tax IDs, including the EIN and the SSN. For TIN matching, see 6.4.

These fields are allowed to be empty so that sole proprietors and new companies from EC-4 fit (Rule 1, see 2.2).

---

## 6. `Checks`: what the existing system already ran

Before handing the case to us, the existing system has already called a KYB service to run four checks. Each one only says whether things matched, not why; finding out why is the agent's job. Think of it as the lab report from a checkup: each line only says normal, high or low, and when something is high, the doctor goes looking for the cause. A test that isn't on the report simply wasn't run, which doesn't mean it's normal.

The memo can't cite `checks`: they have no evidence IDs, so the memo cites the R records the Records worker finds.

### 6.1 The systems behind these checks

Official systems:

| System | What it records | When it updates | Where it falls out of sync | Records worker tools | Matching check | ECs |
|---|---|---|---|---|---|---|
| State registry, run by the Secretary of State (SoS); some states call this office something else, such as the Division of Corporations | The company's legal name, entity ID, type, formation date, status, the people who run it, and its filing history | When the company itself files formation, name change or dissolution paperwork | Sole proprietors aren't in it; a company formed in another state has to be looked up in that state | `sos_search`, `sos_get_entity`, `sos_filing_history` | `checks.sos_name` | EC-1, EC-4 |
| IRS (Internal Revenue Service, the US federal tax agency) | The name behind each tax ID (EIN, SSN) | Created when the EIN is issued; a name change has to be reported to the IRS separately | If a name change isn't reported, the IRS keeps the old name; a new EIN, and a newly reported name change, both take a while to show up in TIN matching | `tin_match` | `checks.tin` | EC-2 |
| DBA registry (state or county); in many states DBAs are registered at the county level, with the county clerk | Who each trade name belongs to (the registrant), the filing date and the expiration date | When the merchant registers or renews | A DBA registered under an old name doesn't follow the company's name change; some states register DBAs at the county level (for example, sole proprietorships and general partnerships in New Jersey), which the verification services the existing system uses may not cover; DBAs can expire | `dba_lookup` | `checks.dba` | EC-1 |

Third-party services:

| Service | What it does | Notes | In the project | ECs |
|---|---|---|---|---|
| KYB services (such as Middesk) | Check the state record, DBAs, TIN matching and more in one go, and return ready-made labels | The values in our `checks` are modeled on labels from services like these. Public sources listed Affirm as a Middesk customer in 2022; we don't know whether it still is | Produces `checks`, which in turn produce `flag_reasons` | All |
| Commercial databases (LexisNexis, Dun & Bradstreet, TLO, etc.) | Combine public records and commercial data into business profiles | Slow to update; new companies often don't show up for months. Affirm's blog says it verifies merchants with databases like LexisNexis and TLO | The Records worker uses `commercial_db_search`; the existing system's result is in `checks.commercial_db` | EC-4 |

Every result the Records worker gets from these systems becomes a piece of evidence with an R ID (see 2.5).

### 6.2 `NameCheck`: does the name match the state record? (`checks.sos_name`)

| `result` | `matched_name` | Meaning | Flag it raises |
|---|---|---|---|
| Verified | Present | The applied name matches the state record | — |
| Similar Match | Present | Similar, but not identical | `NAME_SIMILAR_MATCH` |
| Unverified | Present | The company was found by entity ID, but the name doesn't match | `NAME_UNVERIFIED` (proposed rule, to confirm) |
| Unverified | null | Not found by name or by entity ID | `ENTITY_NOT_FOUND` (proposed rule, to confirm) |

`matched_name` is the name exactly as the state record shows it, such as `NORTHLINE BICYCLE WORKS, L.L.C.`. Intake normalizes both it and `legal_name`; if they come out the same, it marks `NAME_SIMILAR_MATCH` resolved.

Related things the Records worker looks into next:

| Term | What it means | In the project |
|---|---|---|
| Entity suffix | Endings like LLC, L.L.C., Inc. and Corp. that show the entity type | Standardized when Intake normalizes names |
| Name normalization | Making case consistent, removing punctuation and standardizing suffixes before comparing. Many states don't treat two names as different if they differ only in suffix, punctuation, capitalization or spacing, so names that differ only this way can safely be treated as the same name | Done in Intake |
| Entity candidates | Searching the state records by name can return several similarly named companies; the right one has to be picked out | `entity_candidates` and `selected_entity`, returned by the Records worker |
| Entity status (Active / Dissolved) | Whether the company is still valid in the state records; Dissolved means it has been closed | `selected_entity.status`, returned by the Records worker |
| Filing history | Every document the company has filed with the state, including name changes and dissolution | `sos_filing_history` |
| Former name | The legal name before a name change, kept in the filing history | `name_history`, returned by the Records worker |
| Foreign qualification | A company formed in one state (often Delaware) has to register again in each other state where it operates | One reason a company can't be found in the state where it operates |

### 6.3 `DbaCheck`: can the DBA be tied to this business? (`checks.dba`)

| `result` | Meaning | Flag it raises |
|---|---|---|
| Verified | The DBA can be tied to this business | — |
| Unverified | It can't | `DBA_UNVERIFIED` |

This check exists only when the application lists a DBA. Common causes: the DBA is registered under the company's old name, it's registered at the county level where the existing system can't find it, it has expired, or it was never registered. See 9.3 for an example.

### 6.4 `TinCheck`: does the IRS accept this TIN with this name? (`checks.tin`)

| `result` | Meaning | Flag it raises |
|---|---|---|
| Match | The TIN and name combination matches | — |
| Mismatch | The IRS has the TIN, but not with this name | `TIN_MISMATCH` |
| Not Found | The IRS has no record of the number | `TIN_NOT_FOUND` |
| Not Issued | The number hasn't been issued yet; common for brand-new EINs | `TIN_NOT_ISSUED` |

This check is empty when there's neither an EIN nor an SSN. When a sole proprietor has no EIN, the existing system checks the owner's SSN instead, which is why `case_2001` has a `tin` result with `tin_type` = `SSN`.

| Term | What it means | Example in the project |
|---|---|---|
| TIN matching | An IRS service for payers that checks whether a TIN and name combination is correct. KYB services use it | `tin_match`, `checks.tin` |
| Name control | When matching, the IRS compares only the first four characters of the name (spaces don't count). If the EIN was applied for under a business name, it's the first four characters of that name; for individuals, including sole proprietors, it's the first four letters of the last name. DBAs aren't used. So a formatting difference (LLC vs. L.L.C.) usually doesn't affect TIN matching, but a change at the start of the name does | Northline: the IRS has the old name, HARB, while the new name on the application gives NORT, so they don't match (see 9.4). Maya Torres: the IRS has TORR, so comparing it with the shop name Bluebird Tailoring (BLUE) won't match (`case_2001`, see 9.4) |
| Backup withholding | Payers report what they pay each merchant to the IRS under the merchant's TIN and name. If those don't match, the IRS may require the payer to hold back part of what it pays the merchant | One reason payers care about TIN matching (EC-2) |

A few notes:

- **New EINs take time:** an EIN can be used to open a bank account right away, but the IRS says it can take up to two weeks to pass TIN matching, and it can take longer.
- **Not Found and Not Issued may look the same:** real IRS TIN matching responses include "matches", "doesn't match" and "TIN entered is not currently issued", but no separate "not found". A new EIN also can't be found before it reaches the IRS's permanent records. We still need to agree as a team on how to tell these two labels apart.
- **Hard rule:** when a new company's EIN is `Not Issued`, Checks won't let the TIN flag be marked resolved.

### 6.5 `CommercialCheck`: is the company in the commercial databases? (`checks.commercial_db`)

| `result` | Meaning | Flag it raises |
|---|---|---|
| Found | In the commercial databases | — |
| Not Found | Not in the commercial databases | `NO_COMMERCIAL_RECORD` |

This check is empty when the existing system didn't run it, as in `case_1042`. Commercial databases update slowly, so new companies often don't show up for months, and sole proprietors and very small businesses may never have a record.

### 6.6 `Checks`: the four together

| Field | When it's empty |
|---|---|
| `sos_name` | Never; every application has a name |
| `dba` | The application doesn't list a DBA |
| `tin` | There's neither an EIN nor an SSN |
| `commercial_db` | The existing system didn't run this check |

The three sample cases side by side:

| | `case_1042` | `case_2001` | `case_3001` |
|---|---|---|---|
| `sos_name` | Similar Match, with a `matched_name` | Unverified, `matched_name` is null | Verified |
| `dba` | Unverified | Unverified | Empty (no DBA listed) |
| `tin` | Mismatch | Mismatch (no EIN, so the SSN was checked) | Mismatch |
| `commercial_db` | Empty (not run) | Not Found | Found |

- **Empty and Not Found are different:** empty means the check wasn't run; Not Found means it ran and found nothing.
- **`checks` and `flag_reasons` should agree:** `checks` holds the raw results and `flag_reasons` the labels derived from them, plus flags that don't come from these four checks (see 4.2). The two should line up; for example, if `commercial_db` is Not Found, `flag_reasons` should include `NO_COMMERCIAL_RECORD`. But the contract only checks structure, not this kind of agreement (Rule 1).
- **Same name, different things:** `CaseInput.checks` is what the existing system ran before the agent starts; the Checks step in the workflow (see 1.1) is the hard-rule check code runs after the agent writes the memo. Both are called checks, so keep them apart when reading code and docs.

---

## 7. Case evidence: `Document`, `BankVerification`, `WebSnapshot`, `OwnerKYC`

These four parts are the evidence that arrives with the case, with IDs starting with D, B, W and K (see 2.5). The first three are merchant-side, and the Evidence worker reads them. `OwnerKYC` comes from a third-party KYC vendor, and the Records worker reads it. All of them can be empty: a case with no documents or no website snapshot is normal, not an error.

### 7.1 `Document`: documents the merchant uploaded

After the merchant uploads documents, the existing system runs OCR and extracts fields. For each document we get the four fields below; we don't get the original file, and the full OCR text stays in case_db.

| Field | Meaning | Format | Notes |
|---|---|---|---|
| `id` | Evidence ID | `D` plus digits, such as `D1` | Assigned by the existing system; never changes |
| `type` | Document type | See 4.4 | — |
| `extracted` | Fields the existing system extracted | A dictionary; the keys are in 4.5 | — |
| `confidence` | How confident the extraction is | 0 to 1 | Becomes the finding's confidence level; see the note below the table |

The workers don't re-read documents. When confidence is low, the Evidence worker reports the fields as low-confidence findings, and the agent can only say the document is unclear and recommend request_documents, so the merchant uploads a clearer copy. (Re-reading the OCR text wouldn't help much anyway: if the OCR read `HARBOR` as `HARB0R`, the text has the same mistake.)

`known_fields_only` is `Document`'s own check: if `extracted` has a field this document type shouldn't have (say `name` misspelled as `nmae`), it raises an error. The rule is in 4.5.

This evidence doesn't go in `documents`:

| Evidence | Why it isn't a document | Where it goes |
|---|---|---|
| The owner's ID | Handled by the KYC service; we only get the result | `owner_kyc`, see 7.4 |
| Bank statements, voided checks | Handled by the bank verification service; we only get the account name and status | `bank_verification`, see 7.2 |
| Website | The existing system captures a snapshot | `web_snapshot`, see 7.3 |

### 7.2 `BankVerification`: bank verification

Bank verification confirms the payout account exists and belongs to this name. Usually the merchant logs in to online banking to authorize it, or confirms small test deposits.

| Field | Meaning | Format |
|---|---|---|
| `id` | Evidence ID | `B` plus digits |
| `account_holder` | The account name. It's often written as "LEGAL NAME DBA TRADE NAME", which ties the two names together | Text |
| `status` | Verification status: verified means it passed; unverified means it couldn't be verified; pending means the result isn't in yet, for example the test deposits haven't been confirmed; failed means it was tried and didn't pass | One of four values |
| `verified_at` | Date of verification | Date; can be empty |

### 7.3 `WebSnapshot`: website snapshots

| Field | Meaning | Format |
|---|---|---|
| `id` | Evidence ID | `W` plus digits |
| `url` | The page that was captured | Must be a valid URL. The samples use the `.example` domain, which is reserved for examples and isn't a real site |
| `captured_at` | The date the page was captured. When `evidence_index` is built, it becomes this evidence's `as_of` date | Date |
| `extracted` | Fields the existing system extracted from the page | A dictionary; keys only from `WEB_FIELDS` (`legal_name`, `dba`, `address`); can be empty | The merchant's own claim, so always low confidence. The page text stays in case_db (see 2.4) |

`WebSnapshot` has the same `known_fields_only` check as `Document`: a key outside `WEB_FIELDS` raises an error.

Why there's no `confidence`: a web page is already text, so there's no OCR to score. What can go wrong is an incomplete capture: content that only appears after JavaScript runs, or text inside images, never makes it into the snapshot. So when the snapshot lacks some information, the agent should say "not in the snapshot", not "not on the website".

### 7.4 `OwnerKYC`: the owners' KYC results

KYC (Know Your Customer) means verifying a person: are they real, and do their name, date of birth and SSN line up. It verifies people, not businesses; it matters most for sole proprietors, because the person is the business.

| Field | Meaning | Format |
|---|---|---|
| `id` | Evidence ID | `K` plus digits |
| `owner_ref` | Which owner this result belongs to | Must match the `ref` of someone in `application.owners` |
| `result` | The KYC result: pass means it passed, fail means it didn't, pending means the result isn't in yet | One of three values |

---

## 8. `CaseInput`: the top level

| Field | Meaning | Format | Notes |
|---|---|---|---|
| `case_id` | Case ID | `case_` plus digits, such as `case_1042` | Generated by the existing system; never repeats |
| `status` | Where the case stands | See 4.1 | `pending_review` when the case arrives |
| `flag_reasons` | Why the case was flagged | See 4.2 | At least one: an application with no flags doesn't come to review |
| `application` | The application form | See section 5 | — |
| `checks` | The checks the existing system ran | See section 6 | — |
| `documents` | Uploaded documents | See 7.1 | Can be an empty list |
| `bank_verification` | Bank verification | See 7.2 | Can be empty |
| `web_snapshot` | Website snapshots | See 7.3 | Can be an empty list |
| `owner_kyc` | The owners' KYC results | See 7.4 | Can be an empty list |

`check_integrity` is `CaseInput`'s check on the case as a whole. It looks at three things:

1. **No duplicate evidence IDs:** D, B, W and K IDs are checked together. With a duplicate, a citation in the memo wouldn't know which one it means.
2. **KYC results must belong to real owners:** every `owner_ref` in `owner_kyc` must be someone in `application.owners`.
3. **No full tax IDs:** no full SSN or EIN may appear anywhere in the case (see 4.6).

`Application` has one check of its own: `tin_type` and `tin_last4` are given together or not at all.

---

## 9. An example for each FlagReason

Every example follows the same order:

1. A flow diagram: each step says what the merchant did, and after `└─`, the record that step left behind; the last step is the existing system's comparison and its result.
2. What didn't match: both sides of the comparison. Application fields all live under `application`.
3. The cause, plus other common causes.

Where an existing sample case fits (`case_1042.json` and the others in `contracts/examples/case_input/`), the example uses that merchant; the rest are made up for this doc.

| Flag | EC | What the existing system compares | Cause in the example | Example |
|---|---|---|---|---|
| `NAME_SIMILAR_MATCH` | EC-1 | The applied name and the name on the state record | Only the formatting differs | 9.1 Northline |
| `NAME_UNVERIFIED` | EC-1 | The applied name and the company name behind the entity ID | The brand name was entered as the legal name | 9.2 Larkspur |
| `DBA_UNVERIFIED` | EC-1 | The DBA's registrant and the applied name | The DBA is registered under the company's old name | 9.3 Northline |
| `TIN_MISMATCH` | EC-2 | For the same EIN, the applied name and the name the IRS has | The name change wasn't reported to the IRS | 9.4 Northline |
| `TIN_NOT_FOUND` | EC-2 | The EIN on the application and the IRS records | The EIN was entered wrong | 9.5 Copperleaf |
| `TIN_NOT_ISSUED` | EC-2 | The EIN on the application and the IRS TIN matching database | The EIN is brand new and not in the database yet | 9.6 Tidewater |
| `ENTITY_NOT_FOUND` | EC-4 | The applied name and entity ID, and the state registry | Sole proprietors don't register with the state | 9.7 Maya Torres |
| `NO_COMMERCIAL_RECORD` | EC-4 | The applied name and the commercial databases | A new company not picked up yet | 9.8 Saltmarsh |

### 9.1 `NAME_SIMILAR_MATCH` (EC-1)

In short: the name on the application is very close to the state record, but not identical.

Example: Northline Bicycle Works LLC (`case_1042`)

```
① Registers the company in Illinois (it was renamed later; only the current name matters here)
   └─ State registry: NORTHLINE BICYCLE WORKS, L.L.C., entity ID IL-0458821
        │
        ▼
② Applies to Affirm, writing the name the usual way
   └─ Application: legal_name = Northline Bicycle Works LLC, state_entity_id = IL-0458821
        │
        ▼
③ Existing system compares the applied name with the name on the state record
   └─ Result: checks.sos_name = Similar Match (matched_name: NORTHLINE BICYCLE WORKS, L.L.C.)
   └─ Flag: NAME_SIMILAR_MATCH
```

**What didn't match**

| Application | Record | Result |
|---|---|---|
| `legal_name`: Northline Bicycle Works LLC | State registry: NORTHLINE BICYCLE WORKS, L.L.C. | Differs only in capitalization, the comma, and LLC vs. L.L.C. |

**Cause:** the same name written two ways. State records are often in all caps with punctuation, and merchants rarely copy them exactly.

**Other common causes:** & vs. and; abbreviations (Mfg vs. Manufacturing); a missing "The" at the start; small typos.

**In the project:** Intake normalizes both names (see name normalization in 6.2). If they're then identical, it marks this flag resolved and records it in the memo's `auto_resolved` (flags resolved automatically), so no worker gets it. If they still differ after normalization, the agent takes over: not to compare the names again, but to confirm that the applicant and the company on the state record are the same company (by entity ID, formation date and owners), and to find out why the names differ.

### 9.2 `NAME_UNVERIFIED` (EC-1)

In short: the company was found by entity ID, but the name on the application doesn't match the state record at all.

Example: Larkspur Retail Group LLC, which sells candles under the brand Ember & Wick Candles (made up)

```
① Registers the company in Colorado
   └─ State registry: LARKSPUR RETAIL GROUP LLC, entity ID CO-20241187
        │
        ▼
② Registers a DBA and trades as Ember & Wick Candles
   └─ DBA registry: EMBER & WICK CANDLES, registrant LARKSPUR RETAIL GROUP LLC
        │
        ▼
③ Applies to Affirm, entering the brand name as the legal name; the entity ID is correct and dba is blank
   └─ Application: legal_name = Ember & Wick Candles, state_entity_id = CO-20241187
        │
        ▼
④ Existing system compares the applied name with the company name behind the entity ID
   └─ Result: checks.sos_name = Unverified (matched_name: LARKSPUR RETAIL GROUP LLC)
   └─ Flag: NAME_UNVERIFIED
```

**What didn't match**

| Application | Record | Result |
|---|---|---|
| `state_entity_id`: CO-20241187 | State registry: CO-20241187 | Matches; the company was found |
| `legal_name`: Ember & Wick Candles | Name behind this entity ID: LARKSPUR RETAIL GROUP LLC | Two completely different names |

**Cause:** the merchant mixed up its legal name and its DBA, and entered its public brand as the company name.

**Other common causes:** the company changed its name with the state but applied with the old one; a wrong entity ID that points to another company.

**In the project:** this uses the proposed rule from 6.2 (a `matched_name` means `NAME_UNVERIFIED`), which is still to be confirmed. The Records worker can use `dba_lookup` to check whether the applied name is a DBA this company registered, or `sos_filing_history` to check whether it's a former name. The wrong name usually brings a `TIN_MISMATCH` along too, because TIN matching usually uses the same name.

### 9.3 `DBA_UNVERIFIED` (EC-1)

In short: the DBA on the application can't be tied to this business.

Example: Northline (`case_1042`)

```
① While still called Harbor Light Cycles LLC, the company registers the DBA Northline Cycles
   └─ DBA registry: NORTHLINE CYCLES, registrant HARBOR LIGHT CYCLES LLC
        │
        ▼
② Changes its name with the state to Northline Bicycle Works LLC, without updating the DBA
   └─ State registry: the name becomes the new one; the old name stays in the filing history
   └─ DBA registry: the registrant is still the old name
        │
        ▼
③ Applies to Affirm with its current name and DBA
   └─ Application: legal_name = Northline Bicycle Works LLC, dba = Northline Cycles
        │
        ▼
④ Existing system compares the DBA's registrant with the applied name
   └─ Result: checks.dba = Unverified
   └─ Flag: DBA_UNVERIFIED
```

**What didn't match**

| Application | Record | Result |
|---|---|---|
| `dba`: Northline Cycles | DBA registry: NORTHLINE CYCLES | Matches; the DBA was registered |
| `legal_name`: Northline Bicycle Works LLC | The DBA's registrant: HARBOR LIGHT CYCLES LLC | Doesn't match; the registrant is the company's old name |

**Cause:** the DBA is registered under the old name and wasn't updated after the name change. It's really the same company, but the DBA registry alone doesn't show that.

**Other common causes:** the DBA is registered at the county level, which the verification services the existing system uses can't find (`case_2001`); the DBA has expired; it was never registered; it's registered under an affiliated company.

**In the project:** the Records worker uses `sos_filing_history` to find the name change, which proves the old and new names are the same company. The Reconcile step has a rule: a former name proven by official records (in the Records worker's `name_history`) counts as the same company.

### 9.4 `TIN_MISMATCH` (EC-2)

In short: the IRS has the tax ID, but not with the name on the application.

Example: Northline (`case_1042`)

```
① While still called Harbor Light Cycles LLC, the company gets an EIN from the IRS
   └─ IRS: 00-1234567 belongs to HARBOR LIGHT CYCLES LLC, name control HARB
   └─ Merchant has: a CP 575, also showing the old name
        │
        ▼
② Changes its name with the state, but doesn't tell the IRS
   └─ State registry: new name NORTHLINE BICYCLE WORKS LLC
   └─ IRS: still the old name
        │
        ▼
③ Applies to Affirm with the new name and this EIN
   └─ Application: legal_name = Northline Bicycle Works LLC, tin_type = EIN, tin_last4 = 4567
        │
        ▼
④ Existing system runs TIN matching on the EIN plus the applied name
   └─ Result: checks.tin = Mismatch (the new name gives NORT; the IRS has HARB)
   └─ Flag: TIN_MISMATCH
```

**What didn't match**

| Application | Record | Result |
|---|---|---|
| `tin_last4`: 4567 (EIN) | IRS: the same EIN | Matches; the number exists |
| `legal_name`: Northline Bicycle Works LLC (NORT) | Name the IRS has: HARBOR LIGHT CYCLES LLC (HARB) | Doesn't match; the first four characters differ |

**Cause:** the company didn't tell the IRS about its name change, so the IRS still has the EIN under the old name.

**Other common causes:**

- A sole proprietor uses an SSN, but the existing system compares it with the shop name (`case_2001`: the IRS has TORR, while the shop name gives BLUE).
- The EIN actually belongs to another company (`case_3001`: the 147C shows BLUE RIVER TOOLS INC). The Checks hard rules say this must be escalated (handed to someone more senior).
- A typo at the start of the applied name.
- The name change was just reported to the IRS and the database hasn't caught up.

**In the project:** the Records worker uses `tin_match` to try again with the old name; a match shows the IRS still has the old name. That's how the full example in section 7 of the workflow doc works it out. The call passes only the name to test; the tool gets the case's IRS record by `case_id` and compares the names in code.

### 9.5 `TIN_NOT_FOUND` (EC-2)

In short: the IRS has no record of this tax ID.

Example: Copperleaf Dental Supply Inc., in Georgia (made up)

```
① Gets an EIN from the IRS
   └─ IRS: 00-7305512 belongs to COPPERLEAF DENTAL SUPPLY INC
   └─ Merchant has: a CP 575 with the number 00-7305512
        │
        ▼
② Applies to Affirm, swapping two digits of the EIN, and uploads the CP 575
   └─ Application: tin_type = EIN, tin_last4 = 0512 (full number 00-7350512, kept by the existing system)
   └─ documents: D1 is the CP 575, with ein_last4 = 5512 (full number 00-7305512)
        │
        ▼
③ Existing system runs TIN matching on the application's EIN plus the company name
   └─ Result: checks.tin = Not Found
   └─ Flag: TIN_NOT_FOUND
```

**What didn't match**

| Application | Record | Result |
|---|---|---|
| `tin_last4`: 0512 | IRS: no such number | Not found |
| `tin_last4`: 0512 | CP 575 (D1): `ein_last4` 5512 | Doesn't match; two digits were swapped |

**Cause:** the number was entered wrong on the application.

**Other common causes:** a made-up number; a different number, such as a state tax ID, entered in the EIN field.

**In the project:** the Evidence worker compares `ein_last4` on D1 with the application's `tin_last4` and spots the difference. (A swap that only touches the first five digits wouldn't show in the last 4; then only TIN matching catches it.) Also, real IRS responses have no separate "not found" (see 6.4), so how this label differs from `TIN_NOT_ISSUED` is still to be confirmed.

### 9.6 `TIN_NOT_ISSUED` (EC-2)

In short: the IRS says the number hasn't been issued. Common for brand-new EINs.

Example: Tidewater Kayak Rentals LLC, in North Carolina (made up)

```
① Registers the company with the state
   └─ State registry: TIDEWATER KAYAK RENTALS LLC, Active, formed a few days ago
        │
        ▼
② Right after, applies for an EIN on the IRS website and gets the number on the spot
   └─ IRS: issued 00-5521873, but it isn't in the TIN matching database yet
   └─ Merchant has: a CP 575
        │
        ▼
③ A few days later, applies to Affirm without uploading the CP 575
   └─ Application: tin_type = EIN, tin_last4 = 1873, formation_date = a few days ago
        │
        ▼
④ Existing system runs TIN matching
   └─ Result: checks.tin = Not Issued
   └─ Flag: TIN_NOT_ISSUED
```

**What didn't match**

| Application | Record | Result |
|---|---|---|
| `tin_last4`: 1873 (EIN) | IRS TIN matching: no such number yet | Not found |
| `formation_date`: a few days ago | State registry: formed a few days ago | Matches; the company really is new |

**Cause:** the EIN was just issued and isn't in the IRS TIN matching database yet. The IRS says this can take up to two weeks, and it can take longer (see 6.4).

**Other common causes:** a made-up number gets the same IRS response. So this result alone can't tell a brand-new number from a fake one; look at the formation date, and ideally ask for the CP 575.

**In the project:** Checks has a hard rule that the TIN flag can't be marked resolved in this situation. In the workflow doc's design, the agent recommends request_documents (asking the merchant for more documents): an IRS EIN letter such as the CP 575. A company this new usually also has `NO_COMMERCIAL_RECORD`.

### 9.7 `ENTITY_NOT_FOUND` (EC-4)

In short: no state record is found by name or by entity ID.

Example: Maya Torres, who runs a tailoring shop called Bluebird Tailoring (`case_2001`)

```
① Opens a shop as an individual (a sole proprietorship), so there's nothing to register with the state
   └─ State registry: no record
        │
        ▼
② Registers the shop name with the county clerk
   └─ DBA registry (Kings County, NY): BLUEBIRD TAILORING, registrant MAYA TORRES
        │
        ▼
③ Applies to Affirm. She files taxes under her own SSN and has no EIN
   └─ Application: legal_name = Maya Torres, dba = Bluebird Tailoring
   └─ Application: entity_type = SOLE_PROPRIETORSHIP; tin_type = SSN, tin_last4 = 3456; state_entity_id is empty
        │
        ▼
④ Existing system compares the applied name with the state registry
   └─ Result: checks.sos_name = Unverified (matched_name: null)
   └─ Flag: ENTITY_NOT_FOUND
```

**What didn't match**

| Application | Record | Result |
|---|---|---|
| `legal_name`: Maya Torres | State registry: no record | Not found |
| `state_entity_id`: empty | — | No entity ID to look up |

**Cause:** sole proprietors don't register with the state, so there's never a state record.

**Other common causes:** the company was formed in another state and didn't register (foreign qualification) in the state on the application; the DBA was entered as the legal name with no entity ID; the state or entity ID is wrong.

**In the project:** this also uses the proposed rule from 6.2 (a null `matched_name` means `ENTITY_NOT_FOUND`). For a sole proprietor, not finding a record is normal; the agent ties the person to the shop with the DBA certificate (D1), the W-9 (D2), the KYC result (K1) and the bank account name (B1). `case_2001` also has `DBA_UNVERIFIED` (a county-level DBA the existing system can't find, see 9.3) and `TIN_MISMATCH` (see 9.4).

### 9.8 `NO_COMMERCIAL_RECORD` (EC-4)

In short: the company can't be found in the commercial databases.

Example: Saltmarsh Ceramics LLC, in Maine (made up)

```
① Registers the company with the state
   └─ State registry: SALTMARSH CERAMICS LLC, Active, formed just over a month ago
        │
        ▼
② Gets an EIN; a few weeks later it passes TIN matching
   └─ IRS: 00-6630418 belongs to SALTMARSH CERAMICS LLC
        │
        ▼
③ Applies to Affirm just over a month after forming
   └─ Application: legal_name = Saltmarsh Ceramics LLC, formation_date = just over a month ago
        │
        ▼
④ Existing system checks the commercial databases (the state record and TIN both match)
   └─ Result: checks.commercial_db = Not Found
   └─ Flag: NO_COMMERCIAL_RECORD
```

**What didn't match**

| Application | Record | Result |
|---|---|---|
| `legal_name`: Saltmarsh Ceramics LLC | State registry: SALTMARSH CERAMICS LLC | Matches |
| `legal_name`: Saltmarsh Ceramics LLC | Commercial databases: no record | Not found |

**Cause:** commercial databases update slowly, so a new company takes a while to be added.

**Other common causes:** very small businesses (such as sole proprietors) may never have a record; after a recent name change, the database still has the old name.

**In the project:** it's normal for a new company not to be found. The agent uses the formation date from the state record to show the company really is new.

### 9.9 The two out-of-scope flags

These two flags aren't about records not matching; they're risk questions that need human judgment. Intake marks them out of scope and passes them to the analyst as is, and the agent doesn't handle them.

| Flag | When it appears | Example |
|---|---|---|
| `PRODUCT_CATEGORY_REVIEW` | What the merchant sells falls in a category that needs a closer human look | `case_1042` carries it to show how out-of-scope flags are handled |
| `OFAC_POTENTIAL_MATCH` | The name of the business or an owner is similar to a name on the sanctions list | An owner's name differs from someone on the list by one letter |

---

## 10. Term index

| Term | In short | Section |
|---|---|---|
| Analyst | Makes the final decision | 1.1, 4.1 |
| Analyst decision | The step where the analyst decides | 1.1 |
| Articles of Amendment | Name change filing | 4.4 |
| Articles of Organization / Incorporation | Formation documents | 4.4 |
| Assumed name | Another word for DBA | 5.3 |
| Backup withholding | Holding back payments over a TIN mismatch | 6.4 |
| Bank verification | Confirms the payout account | 7.2 |
| Beneficial owner | Who really owns or controls the business | 5.2 |
| Case service | Stores and serves CaseInput | 2.1 |
| Certificate of Good Standing | State proof the company is valid | 4.4 |
| Checks | Hard-rule checks on the memo | 1.1 |
| Commercial database | Aggregated business profiles | 6.1 |
| Corporation | Inc. or Corp. | 4.3 |
| County clerk | Where many DBAs are registered | 6.1 |
| CP 575 | IRS letter issuing the EIN | 4.4 |
| DBA | Trade name | 5.3 |
| DBA certificate | Proof of DBA registration | 4.4 |
| Decision saved | Saves the decision and updates the status | 1.1 |
| Disregarded entity | Taxed as its owner | 4.3 |
| EIN | Business tax ID | 5.3 |
| Entity candidates | Possible matching companies | 6.2 |
| Entity status | Active or Dissolved | 6.2 |
| Entity suffix | LLC, Inc. and so on | 6.2 |
| Entity type | LLC, corporation and so on | 4.3 |
| Evidence worker | Reads the merchant-side evidence | 1.1 |
| Existing system | Produces CaseInput | 2.1 |
| Fictitious business name | Another word for DBA | 5.3 |
| Filing history | Everything filed with the state | 6.2 |
| Foreign qualification | Registering in another state | 6.2 |
| Formation date | When the company was formed | 5.3 |
| Former name | Name before a change | 6.2 |
| Good standing | Filings and fees up to date | 4.4 |
| Intake | First step: prepares the case | 1.1 |
| IRS | US tax agency | 6.1 |
| KYB | Verifying a business | 1 |
| KYB service | Middesk and similar | 6.1 |
| KYC | Verifying a person | 7.4 |
| Lead | The LLM that assigns work and writes the memo | 1.1 |
| Legal name | Official registered name | 5.3 |
| Letter 147C | IRS EIN verification letter | 4.4 |
| LLC | Limited liability company | 4.3 |
| Manager / Member | Runs / owns an LLC | 5.2 |
| Memo | The agent's report for the analyst | 1.1 |
| Merchant | The applying business | 2.1 |
| Name control | First four characters the IRS compares | 6.4 |
| Name normalization | Standardizing names before comparing | 6.2 |
| OFAC / SDN list | Sanctions list | 4.2 |
| Owner | Owns or runs the business | 5.2 |
| Owner reference | Stands in for the SSN | 5.2 |
| Partnership | Two or more owners | 4.3 |
| Reconcile | Compares the two workers' findings | 1.1 |
| Records worker | Looks up official records | 1.1 |
| Secretary of State (SoS) | State business registration office | 6.1 |
| Senior analyst | Handles escalated cases | 4.1 |
| Single-member LLC | LLC with one member | 4.3 |
| Sole proprietorship | One person, no separate entity | 4.3 |
| SSN | Personal tax number | 2.3, 4.6 |
| State entity ID | State-issued company number | 5.3 |
| TIN | Umbrella term for tax IDs | 5.3, 6.4 |
| TIN matching | IRS check of TIN plus name | 6.4 |
| Trade name | Another word for DBA | 5.3 |
| W-9 | Merchant's tax ID form | 4.4 |
