# affirm-agent golden-set evaluation

Run `smoke-json` · review date 2026-11-23 · model `gpt-4o-mini` · Lead `OpenAILeadLLM` · workers LangGraphWorker, LangGraphWorker · judge off

## Headline

| Metric | Result |
|---|---|
| Cases run | 5 (0 crashed) |
| Decision accuracy | 5/5 (100%) |
| All must_cite items cited | 1/5 (20%) |
| must_cite recall (mean per case) | 40% |
| Forbidden-action violations | 0 of 4 checked (2 undecided — run with --judge) |
| Closed at intake with no worker run (auto_resolved only) | 1 |
| Step-9 checks passed | 5/5 (100%) |
| Within max_follow_up_rounds | 5/5 (100%) |
| **Strict pass** (decision + all citations + no violation) | **1/5 (20%)** |
| Seconds per case (mean / max) | 27 / 46 |

## By edge-case group

| Group | Cases | Decision | All citations | Cite recall | Strict |
|---|---|---|---|---|---|
| combo | 1 | 1/1 (100%) | 1/1 (100%) | 100% | 1/1 (100%) |
| ec1 | 2 | 2/2 (100%) | 0/2 (0%) | 25% | 0/2 (0%) |
| ec2 | 1 | 1/1 (100%) | 0/1 (0%) | 0% | 0/1 (0%) |
| ec4 | 1 | 1/1 (100%) | 0/1 (0%) | 50% | 0/1 (0%) |

## Decision confusion matrix (rows = expected, columns = agent)

| expected \ got | recommend_clear | request_documents | escalate | insufficient_evidence | (none) |
|---|---|---|---|---|---|
| recommend_clear | 4 |  |  |  |  |
| request_documents |  | 1 |  |  |  |

## must_cite misses

`retrieved` = a worker fetched a matching record but the memo didn't cite it; `never retrieved` = no worker looked it up.

| Item kind | Why missed | Count |
|---|---|---|
| `R:sos_filing_history` | retrieved, not cited | 2 |
| `R:sos_registry` | never retrieved | 1 |
| `D1` | retrieved, not cited | 1 |
| `R:irs_tin_match` | retrieved, not cited | 1 |
| `R:sos_registry` | retrieved, not cited | 1 |

## Forbidden-action violations

None found.

## Cases that failed

| Case | Expected | Got | Missing citations | Violations | Stop reason |
|---|---|---|---|---|---|
| case_1102 | recommend_clear | recommend_clear | R:sos_registry[state_entity_id=NJ-2655017]; D1 |  |  |
| case_1108 | recommend_clear | recommend_clear | R:sos_filing_history[former_name=UMBERLEIGH PET SUPPLY LLC] |  | resolved |
| case_2129 | request_documents | request_documents | R:irs_tin_match[tin_match_result=Mismatch]; R:sos_registry[officer_name=Uriel Xu] |  | no_internal_follow_up_left |
| case_4529 | recommend_clear | recommend_clear | R:sos_filing_history[filing_event={'event_id': 'MI-2729167-3', 'event_type': 'reinstatement', 'effective_date': '2026-01-16', 'old_value': 'dissolved', 'new_value': 'active'}] |  | resolved |
