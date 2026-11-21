---
title: "Proposal: Affirm KYB Exception Review Agent"
---

Nov 18, 2026 · v3: narrowed to three KYB edge cases after trainer feedback (v2: Nov 15, 2026; v1: Nov 12, 2026)

**Domain:** Financial services. Before Affirm approves a merchant, automated checks compare the merchant's application with state business records, the IRS and other databases. When something doesn't match, the case goes to manual review. For three common kinds of mismatch, an agent finds out why, writes a memo with cited evidence and recommends a decision. An analyst decides.

*\[n\] = confirmed by source n; (A) = our assumption.*

## 1. Company and business context

Affirm lets consumers pay over time for purchases at partner merchants. It earns money from merchant fees, interest, interchange, and selling and servicing loans \[6\].

Core capabilities and the teams that own them:

- **Underwriting each consumer transaction:** credit and risk teams \[6\].
- **Growing the merchant network:** merchant sales \[3\].
- **Controlling merchant risk:** Merchant Risk underwriting, with Compliance \[1\]\[4\].
- **Funding loans:** capital markets \[6\].

This workflow belongs to **controlling merchant risk**. Affirm funds each purchase, so a merchant that isn't who it says it is leaves Affirm with fraud and refund losses (A). Affirm verifies each merchant's identity and registration before approving it \[1\].

## 2. Internal team and workflow

Merchant Risk underwriters and analysts \[4\] do this work. Automated screening runs first. It runs sanctions checks (`ofac_hit`) \[2\], and we assume it also calls a KYB (Know Your Business) service that checks the application against state records, the IRS and commercial databases (A). Services like this return labels such as *Similar Match*, *Unverified*, *Mismatch* or *Not Issued* \[7\]\[8\]. Each check that doesn't match raises a flag, and flagged cases go to manual review (A).

**The agent runs only on flagged cases, and only on three kinds of mismatch.** The numbers come from our list of KYB edge cases; the others are out of scope for this POC.

- **EC-1 Names:** the legal name, a DBA (trade name) or a former name doesn't line up across the application, the state record and the DBA registry.
- **EC-2 TIN:** the tax ID and the name don't match at the IRS, including name-control issues.
- **EC-4 New or unregistered businesses:** a newly formed business or a sole proprietor that the records don't have yet, or never will.

Any other flag, such as a product-category review or a possible sanctions match, goes to the analyst untouched.

After review, the analyst approves, denies (`denied_pbp`), or asks the merchant for more documents \[1\]\[2\]. A case the analyst can't settle goes to a senior analyst (A). Approval unlocks working API keys, and payouts can be held until every review item is resolved \[3\].

## 3. How it works without AI

Underwriters search identity databases and state business records by hand \[1\]. For a mismatch, an analyst pulls the state record and its filing history, checks DBA filings, compares the name on the merchant's IRS EIN letter and bank account, and runs the TIN match again with a corrected name, until everything points to one legal entity (A). A review takes several days \[1\]\[3\].

## 4. Pain points and business impact

- A merchant can't sell with Affirm until it is approved, so slow reviews delay revenue \[3\].
- `prelaunch` merchants already take real Affirm loans while their review waits \[2\].
- Many of these mismatches have harmless causes, such as an EIN too new to pass TIN matching \[8\]\[10\] or a rename the IRS hasn't caught up with (A). The flag alone doesn't say which, so analysts repeat the same lookups for every case (A).
- A few harmless-looking mismatches hide a real problem, such as an EIN that belongs to another company. Clearing one of those by mistake is the costly error (A).
- Affirm is hiring for merchant-risk automation \[4\]\[5\].

## 5. What the agent does

**Scope:** KYB verification for EC-1, EC-2 and EC-4. Product legality, sanctions, ownership structures, addresses, forged documents, disputes, financial statements, pricing and ongoing monitoring are out of scope.

The flags it handles:

| Edge case | Flags | Common harmless causes | What must be ruled out |
|---|---|---|---|
| EC-1 Names | `NAME_SIMILAR_MATCH`, `NAME_UNVERIFIED`, `DBA_UNVERIFIED` | A formatting difference; a brand entered as the legal name; a DBA filed under a former name | A different company with a similar name |
| EC-2 TIN | `TIN_MISMATCH`, `TIN_NOT_FOUND`, `TIN_NOT_ISSUED` | The IRS still has a former name; a sole proprietor's SSN; a typo; a brand-new EIN | An EIN that belongs to another company; a made-up number |
| EC-4 New or unregistered | `ENTITY_NOT_FOUND`, `NO_COMMERCIAL_RECORD` | A sole proprietor has nothing to register with the state; a new company isn't in commercial databases yet | A business that doesn't exist |

The harmless causes follow from how these systems work. The IRS builds each name control, its short code for a taxpayer's name, from the legal name: at most four characters, and never from the DBA \[9\]. A new EIN can take up to two weeks to pass TIN matching \[10\]. Sole proprietors may not show up in authoritative data sources \[12\].

**Design:** one lead agent and two workers, built in LangGraph. The steps are numbered as in `docs/workflow-handoffs.md`.

1. **Case JSON.** The case service serves the flagged case as a `CaseInput`, the format defined in `contracts/`.
2. **Intake (code).** Normalizes names, computes IRS name controls, resolves flags that are only formatting differences, and sets aside out-of-scope flags.
3. **Lead: plan (LLM).** Writes questions for each worker. It never sees text written by the merchant.
4. **Records worker** (its own loop of tool calls). Searches official records: the state registry and filing history, DBA records, IRS TIN matching and commercial databases.
5. **Evidence worker** (its own loop, in parallel with step 4). Reads what the merchant provided: documents, bank verification, website snapshots and owner KYC results. It is the only part that reads merchant-written text.
6. **Reconcile (code).** Lines up the two findings field by field and marks the conflicts.
7. **Lead: follow up (LLM).** When a conflict remains, sends one targeted question to one worker, for at most two rounds.
8. **Lead: write memo (LLM).** For each flag: the outcome, the root cause, the causes ruled out and the evidence IDs. Then one recommendation: recommend_clear, request_documents, escalate or insufficient_evidence.
9. **Checks (code).** Looks up every cited evidence ID and enforces the hard rules. A memo that fails goes back to the lead once.
10. **Analyst decision.** The workflow pauses until the analyst approves, requests documents, escalates or denies.
11. **Decision saved.** The decision, the new case status and the audit log are saved together. Only this step changes a case's status.

What happens after a document request is still to decide: the case either goes back to review or is replaced by a new case.

**Hard rules in code (A):**

- Clearing a flag needs both an official record and independent evidence from the merchant.
- An EIN that belongs to an unrelated company forces an escalation.
- A TIN flag can't be cleared while the IRS says the EIN is `Not Issued`.
- No SSN may appear in a case or a memo. Owners appear as references such as `owner:P1`, and our system never stores the full number (TIN matching uses the case's IRS record, looked up by case ID), so SSNs never reach the model provider.
- Every evidence ID in the memo must exist in its saved source. Excerpts come from code, never from the model.

**Why multiple agents:**

- The two workers cover two kinds of evidence: official records, and what the merchant provides. The first hard rule needs both, and keeping them apart until they are compared mirrors how an analyst works (A).
- Untrusted text stays inside the Evidence worker. Hidden instructions in a merchant's website or documents can distort at most the evidence finding, and that finding still has to pass the code checks.
- Each worker can be tested on its own.
- We will test against a single agent that has all the tools and the same code checks. We expect similar accuracy but weaker resistance to hidden instructions. If that doesn't hold, we simplify.

## 6. Why an agent is needed

- The same flag has different causes that need different lookups. A `TIN_MISMATCH` can be a rename the IRS hasn't caught up with (match again with the former name), a sole proprietor's SSN (match with the owner's name), or an EIN that belongs to someone else (escalate). Because the name control comes from the start of the legal name and never from the DBA \[9\], a rename or a brand used as the legal name can be enough to fail the match (A).
- Official records and the merchant's evidence often disagree, and only a targeted follow-up settles it, for example whether the old name on an EIN letter is a former name or a different company.
- The analyst needs reasoning backed by evidence they can check, and a wrong clear is costly. So the agent cites everything and stops when it isn't sure.
- It is specific to Affirm because Affirm *lends* against each purchase, and `prelaunch` exposure is already live \[2\]. Affirm already asks merchants for the documents the agent reasons about: an IRS EIN letter such as the 147C when the EIN changes, and the state-approved filing when the name changes \[11\].

## 7. Data and systems used

| Data | Internal system at Affirm | In our POC |
|---|---|---|
| Applications, flags, case statuses | Merchant Portal, onboarding and screening \[2\]\[3\] | Generated golden cases |
| KYB check results (name, DBA, TIN, commercial databases) | A KYB service (A) | Generated `checks` in each case, modeled on vendor labels \[7\]\[8\] |
| Business registration and filing history | Secretary of State \[1\] | Mocked state registry |
| DBA registrations | State and county DBA records (A) | Mocked |
| TIN matching | IRS TIN Matching, through a KYB service (A) | Mocked `tin_match` tool |
| Commercial business records | LexisNexis, TLO \[1\] | Mocked |
| Merchant documents: IRS EIN letters, W-9, formation and amendment filings, DBA certificates | Merchant uploads \[1\] | Generated: extracted fields plus text |
| Bank verification, website snapshots, owner KYC results | Onboarding checks (A) | Generated, including pages with hidden instructions |

All POC data is fictional and needs no outside account. EINs start with `00` and test SSNs with `000`, which real numbers never do. Social Security numbers and bank account numbers are never sent to the model provider.

## 8. Human approval

| Action | Who |
|---|---|
| Lookups, findings, drafting the memo and the document request | Agent |
| Approve, request documents, escalate, deny | Analyst |
| Decide escalated cases | Senior analyst (A) |
| Manage users | Admin. Not in the POC: the auth service creates the demo users when it starts (`docs/auth.md`) |
| Change a case's status, contact the merchant, issue API keys, release payouts | Never the agent |

Roles are enforced on the backend. The POC keeps two roles, `analyst` and `senior_analyst`; `docs/auth.md` lists what each may do and where each rule is checked.

## 9. How to measure success

We use a golden dataset of about 28 flagged cases, 9–10 written by each team member, each set including one case with hidden instructions. Every case lists the expected recommendation, the evidence the memo must cite, and the actions it must never take (A).

**Metrics:**

- false-clear rate: flags cleared that should not have been, with zero hard-rule misses
- recommendation accuracy
- every citation is real and supports its claim
- correct escalation when a case is uncertain
- comparison with the single agent
- cost, time and override rate, measured with LangFuse

**Cases:**

- **EC-1:** a formatting-only difference, a brand entered as the legal name, and a DBA filed under a former name.
- **EC-2:** a rename the IRS hasn't caught up with, a sole proprietor's SSN, a mistyped EIN, a brand-new EIN, and an EIN that belongs to another company.
- **EC-4:** a sole proprietor with no state record, and a new company not yet in commercial databases.
- **Combined:** one rename that raises EC-1 and EC-2 flags together.
- **Unsafe:** hidden instructions in website or document text, and an analyst going beyond their role.
- **Uncertain:** conflicts that remain after two follow-ups, which should come back as insufficient_evidence.

## 10. What the engineering team builds

- **Auth service and API gateway:** JWT login, roles, and checks on every endpoint.
- **Case service:** cases, decisions and the audit log, plus the seed script that loads the golden cases.
- **Agent service:** the LangGraph workflow (intake, lead, two workers, reconcile, checks and the human-approval interrupt) with saved checkpoints, and the `/reviews` API.
- **Worker tools and mock records:** read-only tools over the mocked official records.
- **Review app:** a queue filtered by role, the memo with clickable evidence, and the decision form.
- **Evaluation and LangFuse tracing:** the golden-dataset runner, including the single-agent comparison.
- **Deployment:** Docker Compose on EC2.

Who builds what, and what passes between the steps, is in `docs/workflow-handoffs.md`. How the parts fit together is in `docs/architecture.md`.

## Sources

Sources 1–6 were accessed Nov 12, 2026, and sources 7–12 on Nov 18, 2026.

1. Affirm Business Blog, ["Underwriting basics: How we evaluate and approve retail partners"](https://affirm.com/business/blog/underwriting-basics-how-we-evaluate-and-approve-retail-partners)
2. Affirm Docs, ["Programmatic Merchant Onboarding API and Webhooks"](https://docs.affirm.com/platforms/docs/programmatic-merchant-onboarding-api-and-webhooks)
3. Affirm Business Hub, [merchant requirements and fees](https://businesshub.affirm.com/hc/en-us/articles/5995432933652) and [signing up and application FAQ](https://businesshub.affirm.com/hc/en-gb/articles/5997099608724)
4. Affirm job posting, ["Merchant Risk Underwriter"](https://www.builtinchicago.org/job/finance/merchant-risk-underwriter/111661)
5. Affirm job posting, ["Analyst I, Merchant Risk"](https://www.builtinnyc.com/job/analyst-i-merchant-risk/8127165) (read from the search summary only)
6. Affirm Investor Relations, ["Investor FAQs"](https://investors.affirm.com/investor-resources/investor-faqs)
7. Middesk Docs, [name and DBA review tasks](https://docs.middesk.com/verify-business/name-address/review-tasks)
8. Middesk Docs, [TIN review tasks](https://docs.middesk.com/verify-business/tin/review-tasks)
9. IRS, ["Using the correct name control in e-filing corporate tax returns"](https://www.irs.gov/businesses/corporations/using-the-correct-name-control-in-e-filing-corporate-tax-returns)
10. IRS, ["Employer identification number"](https://www.irs.gov/businesses/employer-identification-number)
11. Affirm Business Hub, ["Legal entity changes for your business"](https://businesshub.affirm.com/hc/en-us/articles/15859874981524-Legal-entity-changes-for-your-business)
12. Persona Blog, [how to build a KYB process](https://withpersona.com/blog/how-to-build-a-know-your-business-kyb-process/)
