# affirm-agent golden-set evaluation

Run `final-full` · review date 2026-11-23 · model `gpt-4o-mini` · Lead `OpenAILeadLLM` · workers LangGraphWorker, LangGraphWorker · judge on

## Headline

| Metric | Result |
|---|---|
| Cases run | 119 (0 crashed) |
| Decision accuracy | 103/119 (87%) |
| All must_cite items cited | 82/119 (69%) |
| must_cite recall (mean per case) | 84% |
| Forbidden-action violations | 7 of 131 checked (0 undecided) |
| Closed at intake with no worker run (auto_resolved only) | 5 |
| Step-9 checks passed | 119/119 (100%) |
| Within max_follow_up_rounds | 119/119 (100%) |
| **Strict pass** (decision + all citations + no violation) | **69/119 (58%)** |
| Seconds per case (mean / max) | 34 / 77 |

## By edge-case group

| Group | Cases | Decision | All citations | Cite recall | Strict |
|---|---|---|---|---|---|
| combo | 30 | 21/30 (70%) | 24/30 (80%) | 90% | 18/30 (60%) |
| ec1 | 29 | 29/29 (100%) | 16/29 (55%) | 71% | 13/29 (45%) |
| ec2 | 30 | 24/30 (80%) | 20/30 (67%) | 87% | 17/30 (57%) |
| ec4 | 30 | 29/30 (97%) | 22/30 (73%) | 86% | 21/30 (70%) |

## Decision confusion matrix (rows = expected, columns = agent)

| expected \ got | recommend_clear | request_documents | escalate | insufficient_evidence | (none) |
|---|---|---|---|---|---|
| recommend_clear | 61 | 6 |  |  |  |
| request_documents |  | 32 | 2 |  |  |
| escalate |  | 7 | 10 |  |  |
| insufficient_evidence |  | 1 |  |  |  |

## must_cite misses

`retrieved` = a worker fetched a matching record but the memo didn't cite it; `never retrieved` = no worker looked it up.

| Item kind | Why missed | Count |
|---|---|---|
| `R:sos_registry` | never retrieved | 20 |
| `K1` | retrieved, not cited | 6 |
| `D1` | retrieved, not cited | 4 |
| `R:sos_filing_history` | never retrieved | 3 |
| `R:irs_tin_match` | never retrieved | 2 |
| `R:commercial_db` | never retrieved | 2 |
| `R:sos_registry_search` | never retrieved | 2 |
| `R:irs_tin_match` | retrieved, not cited | 1 |
| `R:county_dba` | never retrieved | 1 |
| `R:sos_filing_history` | retrieved, not cited | 1 |

## Forbidden-action violations

| Action | Decided by | Cases |
|---|---|---|
| treat the look-alike entity as the merchant's because the names are similar | judge | 2 |
| explain the mismatch as a name-control formatting issue | judge | 2 |
| treat the expired filing as proof the DBA is current | judge | 1 |
| treat the other company's DBA filing as the merchant's | judge | 1 |
| explain the missing record as a new-business lag | judge | 1 |

## Cases that failed

| Case | Expected | Got | Missing citations | Violations | Stop reason |
|---|---|---|---|---|---|
| case_1102 | recommend_clear | recommend_clear | R:sos_registry[state_entity_id=NJ-2655017]; D1 |  |  |
| case_1103 | recommend_clear | recommend_clear | R:sos_registry[state_entity_id=OH-2544040] |  |  |
| case_1104 | recommend_clear | recommend_clear | R:sos_registry[state_entity_id=MI-2729023]; D1 |  |  |
| case_1105 | recommend_clear | recommend_clear | R:sos_registry[state_entity_id=FL-2211021]; R:irs_tin_match[tin_match_result=Match] |  |  |
| case_1106 | recommend_clear | recommend_clear | R:sos_registry[state_entity_id=MA-2507022]; R:irs_tin_match[tin_match_result=Match] |  |  |
| case_1107 | recommend_clear | recommend_clear | R:irs_tin_match[tin_match_result=Match] |  | resolved |
| case_1110 | recommend_clear | recommend_clear | R:sos_filing_history[former_name=OLDCASTLE TEA COMPANY LLC] |  | resolved |
| case_1111 | recommend_clear | recommend_clear | D1 |  | resolved |
| case_1112 | recommend_clear | recommend_clear | D1 |  | resolved |
| case_1117 | request_documents | request_documents |  | treat the expired filing as proof the DBA is current | no_internal_follow_up_left |
| case_1119 | request_documents | request_documents | K1 |  | no_internal_follow_up_left |
| case_1120 | request_documents | request_documents | K1 |  | no_internal_follow_up_left |
| case_1122 | escalate | escalate | R:commercial_db | treat the other company's DBA filing as the merchant's | hard_fail |
| case_1123 | escalate | escalate | R:commercial_db |  | hard_fail |
| case_1124 | escalate | escalate |  | treat the look-alike entity as the merchant's because the names are similar | hard_fail |
| case_1126 | escalate | escalate |  | treat the look-alike entity as the merchant's because the names are similar | hard_fail |
| case_2107 | recommend_clear | recommend_clear | K1 |  | resolved |
| case_2108 | recommend_clear | recommend_clear | K1 |  | resolved |
| case_2109 | recommend_clear | recommend_clear | K1 |  | resolved |
| case_2110 | escalate | **request_documents** | R:sos_registry[state_entity_id=TN-2692094] | explain the mismatch as a name-control formatting issue | no_internal_follow_up_left |
| case_2111 | escalate | **request_documents** | R:sos_registry[state_entity_id=PA-2581045] | explain the mismatch as a name-control formatting issue | no_internal_follow_up_left |
| case_2112 | escalate | **request_documents** | R:sos_registry[state_entity_id=NY-2174043] |  | no_internal_follow_up_left |
| case_2113 | request_documents | request_documents | R:sos_registry[state_entity_id=MA-2507034] |  | no_internal_follow_up_left |
| case_2114 | request_documents | request_documents | R:sos_registry[state_entity_id=IL-2248018] |  | no_internal_follow_up_left |
| case_2115 | request_documents | request_documents | R:sos_registry[state_entity_id=CO-2322055] |  | resolved |
| case_2116 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_2117 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_2120 | request_documents | **escalate** |  |  | hard_fail |
| case_2126 | recommend_clear | recommend_clear | R:county_dba |  | resolved |
| case_4513 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900028] |  | no_internal_follow_up_left |
| case_4514 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900033] |  | no_internal_follow_up_left |
| case_4515 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900058] |  | no_internal_follow_up_left |
| case_4522 | escalate | escalate | R:sos_registry_search |  | hard_fail |
| case_4523 | escalate | escalate |  | explain the missing record as a new-business lag | hard_fail |
| case_4524 | escalate | escalate | R:sos_registry_search; K1 |  | hard_fail |
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
| case_5117 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900108] |  | no_internal_follow_up_left |
| case_5118 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900130] |  | no_internal_follow_up_left |
| case_5127 | escalate | **request_documents** |  |  | no_internal_follow_up_left |
| case_5128 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5129 | insufficient_evidence | **request_documents** |  |  | no_internal_follow_up_left |
