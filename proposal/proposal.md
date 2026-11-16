---
title: "Proposal: Affirm Merchant Risk Review Agent"
---

Nov 15, 2026 · Revised after trainer feedback (v1: Nov 12, 2026)

**Domain:** Financial services. For merchant applications that Affirm's automated screening sends to manual review, an agent checks the merchant's identity and whether its products can be financed. It writes a risk memo with cited evidence and recommends a decision. An underwriter decides.

*\[n\] = confirmed by source n; (A) = our assumption.*

## 1. Company and business context

Affirm lets consumers pay over time for purchases at partner merchants. It earns money from merchant fees, interest, interchange, and selling and servicing loans \[6\].

Core capabilities and the teams that own them:

- **Underwriting each consumer transaction:** credit and risk teams \[6\].
- **Growing the merchant network:** merchant sales \[3\].
- **Controlling merchant risk:** Merchant Risk underwriting, with Compliance \[1\]\[4\].
- **Funding loans:** capital markets \[6\].

This workflow belongs to **controlling merchant risk**. Affirm funds each purchase, so a bad merchant leaves it with fraud and refund losses (A).

## 2. Internal team and workflow

Merchant Risk underwriters and analysts \[4\] do this work. Automated screening already runs sanctions checks (`ofac_hit`) \[2\]. We assume it also clears low-risk merchants on its own (A). **The agent runs only on cases flagged for manual review.** The flags (A) are:

- an identity or registration mismatch
- a partial OFAC name match
- a restricted, unclear or high-risk product category
- missing information

After review, the underwriter approves, denies (`denied_pbp`), or asks the merchant for more information \[1\]\[2\]. Sanctions questions go to Compliance, and high-exposure merchants need a Senior Underwriter (A). Approval unlocks working API keys, and payouts can be held until every review item is resolved \[3\].

## 3. How it works without AI

Underwriters search identity databases and state business records by hand, and compare sanctions entries. They also read the merchant's website against the Prohibited Business Policy and FDA/FTC rules \[1\]. A review takes several days \[1\]\[3\].

## 4. Pain points and business impact

- A merchant can't sell with Affirm until it is approved, so slow reviews delay revenue \[3\].
- `prelaunch` merchants already take real Affirm loans while their review waits \[2\].
- Analysts repeat the same lookups even when a flag is harmless, and they record decisions inconsistently (A).
- Affirm is hiring for merchant-risk automation \[4\]\[5\].

## 5. What the agent does

**Scope:** two underwriting factors \[1\]. **Merchant verification** covers identity, registration and sanctions near-matches. **Product legality for financing** covers prohibited and restricted products, certificates and resale risk. Disputes, financial statements, pricing, online reviews and ongoing monitoring are out of scope.

**Design:** one lead agent and two workers, built in LangGraph.

1. **Intake (lead agent).** It reads the application and the flags, and lists missing fields.
2. **Route (fixed rule).** Each flag sends the case to a set worker. When the flag is unclear or there are several, both workers run in parallel.
3. **Investigate (workers, each running its own loop of tool calls).**
   - The **Identity & sanctions worker** searches the registry by name, then by owner and address if needed. It compares owners, addresses and dates of birth with any OFAC near-match, and rates the match as a *likely false positive* or a *possible true match*.
   - The **Product & policy worker** reads the storefront, opens risky product pages, gives each product a policy category, and checks uploaded certificates.
4. **Cross-check (lead agent).** It compares shared fields across the findings: legal name, owners, address, declared category and observed category. When they conflict, it sends one follow-up to the right worker. A conflict that is still open gets escalated.
5. **Memo.** The memo gives cited evidence for each factor and one recommendation: approve, request documents, deny, or escalate. Code checks the memo before an analyst sees it.
6. **Human checkpoint.** The workflow pauses until a person decides. After a document request, the status is `awaiting_docs`. When the analyst uploads the merchant's document, the workflow resumes from its checkpoint and reruns only the worker that needed it.

**Hard rules in code (A):**

- A prohibited category ID forces a deny recommendation.
- Every sanctions near-match goes to Compliance, and a possible true match is marked urgent.
- Exposure above $10,000 maximum order value or $1M projected yearly sales needs a Senior Underwriter.
- Every quoted piece of evidence must appear in its saved source.

**Why multiple agents:**

- The two jobs need different tools and data, and they mirror how the work is already split between identity/Compliance and policy review (A).
- Untrusted website text stays inside the Product worker. Hidden instructions on a merchant's site can distort at most the product finding, and that finding still has to pass the code checks.
- Each worker can be tested on its own.
- We will test against a single agent that has all the tools. We expect similar accuracy but weaker resistance to hidden instructions. If that doesn't hold, we simplify.

## 6. Why an agent is needed

- Each flag leads somewhere different. A name mismatch can be a harmless trade name, or it can lead to an owner search that finds a sanctions near-match.
- Judging whether a product is prohibited means reading website text against policy text.
- When the findings conflict, targeted follow-up is needed.
- It is specific to Affirm because Affirm *lends* against each purchase. High-resale goods bought on credit are a fraud risk \[1\], and `prelaunch` exposure is already live \[2\].

## 7. Data and systems used

| Data | Internal system at Affirm | In our POC |
|---|---|---|
| Applications, flags, case statuses | Merchant Portal, onboarding and screening \[2\]\[3\] | Generated |
| Sanctions list | OFAC screening \[2\] | **Real** public OFAC SDN list \[7\] |
| Business registration and owners | LexisNexis, TLO, Secretary of State \[1\] | Mocked registry |
| Storefront and catalog | Merchant website \[1\] | Generated pages, including traps |
| Prohibited and restricted products, resale risk | Policy documents and risk rules \[1\]\[3\] | Written by us |
| Certificates | Merchant uploads \[1\] | Generated |

All POC data is free and needs no outside account. Social Security numbers and bank account numbers are never sent to the model provider.

## 8. Human approval

| Action | Who |
|---|---|
| Lookups, assessments, drafting the memo and document requests | Agent |
| Approve, deny, send document requests, upload documents | Merchant Risk Analyst |
| Approve a high-exposure merchant | Senior Underwriter |
| Clear or confirm a sanctions near-match | Compliance Officer |
| Manage users | Admin |
| Issue API keys, release payouts, edit policy | Never the agent |

Roles are enforced on the backend.

## 9. How to measure success

We use a golden dataset of about 25 flagged merchants, each with a known correct outcome (A).

**Metrics:**

- zero hard-stop misses
- decision and routing accuracy
- every citation is real and supports its claim
- correct escalation when a case is uncertain
- comparison with the single agent
- cost, time and override rate, measured with LangFuse

**Cases:**

- **Normal:** a harmless trade-name mismatch, and a high-exposure merchant.
- **Identity:** an owner mismatch, and sanctions near-matches that are false or possibly true.
- **Product:** a product that is clearly prohibited, one that is prohibited but described vaguely, and a missing certificate that leads to a document request, an upload and then approval.
- **Conflicts and missing information:** a registry that disagrees with the storefront, and missing fields.
- **Unsafe:** hidden instructions in website text, an analyst going beyond their role, and requests for payouts or API keys.
- **Uncertain:** a truly ambiguous case that should go to a person.

## 10. What the engineering team builds

- **Auth service and API gateway:** JWT login, four roles, and checks on every endpoint.
- **Merchant review service:** applications, case statuses, the mocked registry, storefronts, documents, memos and decisions.
- **LangGraph agent service:** the lead agent, two workers, human-approval interrupts and saved checkpoints.
- **Per-agent tools and code checks:** each agent gets its own tools, plus the citation checker and the hard rules.
- **Underwriter console:** a queue filtered by role, the memo with evidence, approvals and document upload.
- **Guardrails, evaluation and LangFuse tracing:** the golden-dataset runner includes the single-agent comparison.

## Sources

Sources 1–6 were accessed Nov 12, 2026, and source 7 on Nov 15, 2026.

1. Affirm Business Blog, ["Underwriting basics: How we evaluate and approve retail partners"](https://affirm.com/business/blog/underwriting-basics-how-we-evaluate-and-approve-retail-partners)
2. Affirm Docs, ["Programmatic Merchant Onboarding API and Webhooks"](https://docs.affirm.com/platforms/docs/programmatic-merchant-onboarding-api-and-webhooks)
3. Affirm Business Hub, [merchant requirements and fees](https://businesshub.affirm.com/hc/en-us/articles/5995432933652) and [signing up and application FAQ](https://businesshub.affirm.com/hc/en-gb/articles/5997099608724)
4. Affirm job posting, ["Merchant Risk Underwriter"](https://www.builtinchicago.org/job/finance/merchant-risk-underwriter/111661)
5. Affirm job posting, ["Analyst I, Merchant Risk"](https://www.builtinnyc.com/job/analyst-i-merchant-risk/8127165) (read from the search summary only)
6. Affirm Investor Relations, ["Investor FAQs"](https://investors.affirm.com/investor-resources/investor-faqs)
7. U.S. Treasury OFAC, [Sanctions List Service: SDN List](https://sanctionslist.ofac.treas.gov/Home/SdnList)
