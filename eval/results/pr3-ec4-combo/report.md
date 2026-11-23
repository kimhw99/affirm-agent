# affirm-agent golden-set evaluation

Run `pr3-ec4-combo` · review date 2026-11-23 · model `gpt-4o-mini` · Lead `OpenAILeadLLM` · workers LangGraphWorker, LangGraphWorker · judge on

## Headline

| Metric | Result |
|---|---|
| Cases run | 60 (0 crashed) |
| Decision accuracy | 50/60 (83%) |
| All must_cite items cited | 40/60 (67%) |
| must_cite recall (mean per case) | 82% |
| Forbidden-action violations | 4 of 66 checked (0 undecided) |
| Closed at intake with no worker run (auto_resolved only) | 0 |
| Step-9 checks passed | 60/60 (100%) |
| Within max_follow_up_rounds | 60/60 (100%) |
| **Strict pass** (decision + all citations + no violation) | **35/60 (58%)** |
| Seconds per case (mean / max) | 30 / 59 |

## By edge-case group

| Group | Cases | Decision | All citations | Cite recall | Strict |
|---|---|---|---|---|---|
| combo | 30 | 22/30 (73%) | 22/30 (73%) | 88% | 17/30 (57%) |
| ec4 | 30 | 28/30 (93%) | 18/30 (60%) | 76% | 18/30 (60%) |

## Decision confusion matrix (rows = expected, columns = agent)

| expected \ got | recommend_clear | request_documents | escalate | insufficient_evidence | (none) |
|---|---|---|---|---|---|
| recommend_clear | 32 | 5 |  |  |  |
| request_documents |  | 12 | 1 |  |  |
| escalate |  | 3 | 6 |  |  |
| insufficient_evidence |  | 1 |  |  |  |

## must_cite misses

`retrieved` = a worker fetched a matching record but the memo didn't cite it; `never retrieved` = no worker looked it up.

| Item kind | Why missed | Count |
|---|---|---|
| `R:sos_registry` | never retrieved | 9 |
| `D1` | retrieved, not cited | 6 |
| `B1` | retrieved, not cited | 5 |
| `R:sos_filing_history` | never retrieved | 3 |
| `R:sos_registry_search` | never retrieved | 2 |
| `K1` | retrieved, not cited | 2 |
| `R:county_dba` | retrieved, not cited | 1 |
| `K2` | retrieved, not cited | 1 |

## Forbidden-action violations

| Action | Decided by | Cases |
|---|---|---|
| explain the missing record as a new-business lag | judge | 2 |
| require a state registration from a general partnership | judge | 1 |
| conclude the company does not exist | judge | 1 |

## Cases that failed

| Case | Expected | Got | Missing citations | Violations | Stop reason |
|---|---|---|---|---|---|
| case_4513 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900028] |  | no_internal_follow_up_left |
| case_4514 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900033] |  | no_internal_follow_up_left |
| case_4515 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900058] |  | no_internal_follow_up_left |
| case_4522 | escalate | escalate | R:sos_registry_search; B1; D1 | explain the missing record as a new-business lag | hard_fail |
| case_4523 | escalate | escalate | K1; B1; D1 |  | hard_fail |
| case_4524 | escalate | escalate | R:sos_registry_search; B1; D1 | explain the missing record as a new-business lag | hard_fail |
| case_4525 | recommend_clear | recommend_clear | D1 |  | resolved |
| case_4526 | recommend_clear | **request_documents** | D1; R:county_dba; K1; K2 | require a state registration from a general partnership | no_internal_follow_up_left |
| case_4527 | recommend_clear | recommend_clear | D1 |  | resolved |
| case_4528 | request_documents | **escalate** | R:sos_filing_history[filing_event={'event_id': 'TX-2100098-2', 'event_type': 'dissolution', 'effective_date': '2026-04-05', 'old_value': 'active', 'new_value': 'dissolved'}] |  | hard_fail |
| case_4529 | recommend_clear | recommend_clear | R:sos_filing_history[filing_event={'event_id': 'MI-2729167-3', 'event_type': 'reinstatement', 'effective_date': '2026-01-16', 'old_value': 'dissolved', 'new_value': 'active'}] |  | resolved |
| case_4530 | recommend_clear | recommend_clear | R:sos_filing_history[filing_event={'event_id': 'NJ-2655126-3', 'event_type': 'reinstatement', 'effective_date': '2026-03-02', 'old_value': 'dissolved', 'new_value': 'active'}] |  | resolved |
| case_5104 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5105 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5106 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5110 | escalate | **request_documents** | R:sos_registry[state_entity_id=OR-2396101] |  | no_internal_follow_up_left |
| case_5111 | escalate | **request_documents** | R:sos_registry[state_entity_id=MN-2618168] |  | no_internal_follow_up_left |
| case_5112 | escalate | **request_documents** | R:sos_registry[state_entity_id=TN-2692144] |  | no_internal_follow_up_left |
| case_5116 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900097] |  | no_internal_follow_up_left |
| case_5117 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900108] | conclude the company does not exist | no_internal_follow_up_left |
| case_5118 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900130] |  | no_internal_follow_up_left |
| case_5126 | escalate | escalate | B1 |  | hard_fail |
| case_5127 | escalate | escalate | B1 |  | hard_fail |
| case_5128 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5129 | insufficient_evidence | **request_documents** |  |  | no_internal_follow_up_left |
