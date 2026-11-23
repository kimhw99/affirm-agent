# affirm-agent golden-set evaluation

Run `pr4-recheck` · review date 2026-11-23 · model `gpt-4o-mini` · Lead `OpenAILeadLLM` · workers LangGraphWorker, LangGraphWorker · judge on

## Headline

| Metric | Result |
|---|---|
| Cases run | 10 (0 crashed) |
| Decision accuracy | 9/10 (90%) |
| All must_cite items cited | 7/10 (70%) |
| must_cite recall (mean per case) | 83% |
| Forbidden-action violations | 1 of 12 checked (0 undecided) |
| Closed at intake with no worker run (auto_resolved only) | 0 |
| Step-9 checks passed | 10/10 (100%) |
| Within max_follow_up_rounds | 10/10 (100%) |
| **Strict pass** (decision + all citations + no violation) | **6/10 (60%)** |
| Seconds per case (mean / max) | 36 / 51 |

## By edge-case group

| Group | Cases | Decision | All citations | Cite recall | Strict |
|---|---|---|---|---|---|
| combo | 2 | 2/2 (100%) | 0/2 (0%) | 50% | 0/2 (0%) |
| ec1 | 3 | 3/3 (100%) | 3/3 (100%) | 100% | 2/3 (67%) |
| ec2 | 2 | 1/2 (50%) | 1/2 (50%) | 67% | 1/2 (50%) |
| ec4 | 3 | 3/3 (100%) | 3/3 (100%) | 100% | 3/3 (100%) |

## Decision confusion matrix (rows = expected, columns = agent)

| expected \ got | recommend_clear | request_documents | escalate | insufficient_evidence | (none) |
|---|---|---|---|---|---|
| recommend_clear | 3 | 1 |  |  |  |
| request_documents |  | 5 |  |  |  |
| escalate |  |  | 1 |  |  |

## must_cite misses

`retrieved` = a worker fetched a matching record but the memo didn't cite it; `never retrieved` = no worker looked it up.

| Item kind | Why missed | Count |
|---|---|---|
| `R:sos_registry` | never retrieved | 2 |
| `D1` | retrieved, not cited | 1 |
| `K1` | retrieved, not cited | 1 |

## Forbidden-action violations

| Action | Decided by | Cases |
|---|---|---|
| treat the look-alike entity as the merchant's because the names are similar | judge | 1 |

## Cases that failed

| Case | Expected | Got | Missing citations | Violations | Stop reason |
|---|---|---|---|---|---|
| case_1124 | escalate | escalate |  | treat the look-alike entity as the merchant's because the names are similar | hard_fail |
| case_2108 | recommend_clear | **request_documents** | D1; K1 |  | resolved |
| case_5117 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900108] |  | no_internal_follow_up_left |
| case_5118 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900130] |  | no_internal_follow_up_left |
