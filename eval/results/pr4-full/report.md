# affirm-agent golden-set evaluation

Run `pr4-full` · review date 2026-11-23 · model `gpt-4o-mini` · Lead `OpenAILeadLLM` · workers LangGraphWorker, LangGraphWorker · judge on

## Headline

| Metric | Result |
|---|---|
| Cases run | 119 (0 crashed) |
| Decision accuracy | 102/119 (86%) |
| All must_cite items cited | 82/119 (69%) |
| must_cite recall (mean per case) | 83% |
| Forbidden-action violations | 9 of 131 checked (0 undecided) |
| Closed at intake with no worker run (auto_resolved only) | 5 |
| Step-9 checks passed | 115/119 (97%) |
| Within max_follow_up_rounds | 119/119 (100%) |
| **Strict pass** (decision + all citations + no violation) | **72/119 (61%)** |
| Seconds per case (mean / max) | 29 / 51 |

## By edge-case group

| Group | Cases | Decision | All citations | Cite recall | Strict |
|---|---|---|---|---|---|
| combo | 30 | 22/30 (73%) | 24/30 (80%) | 90% | 19/30 (63%) |
| ec1 | 29 | 29/29 (100%) | 16/29 (55%) | 71% | 14/29 (48%) |
| ec2 | 30 | 23/30 (77%) | 21/30 (70%) | 88% | 18/30 (60%) |
| ec4 | 30 | 28/30 (93%) | 21/30 (70%) | 83% | 21/30 (70%) |

## Decision confusion matrix (rows = expected, columns = agent)

| expected \ got | recommend_clear | request_documents | escalate | insufficient_evidence | (none) |
|---|---|---|---|---|---|
| recommend_clear | 59 | 8 |  |  |  |
| request_documents |  | 32 | 2 |  |  |
| escalate |  | 6 | 11 |  |  |
| insufficient_evidence |  | 1 |  |  |  |

## must_cite misses

`retrieved` = a worker fetched a matching record but the memo didn't cite it; `never retrieved` = no worker looked it up.

| Item kind | Why missed | Count |
|---|---|---|
| `R:sos_registry` | never retrieved | 20 |
| `K1` | retrieved, not cited | 9 |
| `D1` | retrieved, not cited | 4 |
| `R:sos_filing_history` | never retrieved | 4 |
| `R:irs_tin_match` | never retrieved | 3 |
| `R:commercial_db` | never retrieved | 2 |
| `R:sos_registry_search` | never retrieved | 1 |
| `R:county_dba` | retrieved, not cited | 1 |
| `K2` | retrieved, not cited | 1 |

## Forbidden-action violations

| Action | Decided by | Cases |
|---|---|---|
| explain the mismatch as a name-control formatting issue | judge | 3 |
| treat the other company's DBA filing as the merchant's | judge | 2 |
| treat the look-alike entity as the merchant's because the names are similar | judge | 2 |
| explain the missing record as a new-business lag | judge | 1 |
| require a state registration from a general partnership | judge | 1 |

## Step-9 check failures (final memo)

| Rule | Cases |
|---|---|
| UNSUPPORTED_EXISTENCE_CLAIM | 4 |

## Cases that failed

| Case | Expected | Got | Missing citations | Violations | Stop reason |
|---|---|---|---|---|---|
| case_1102 | recommend_clear | recommend_clear | R:sos_registry[state_entity_id=NJ-2655017]; D1 |  |  |
| case_1103 | recommend_clear | recommend_clear | R:sos_registry[state_entity_id=OH-2544040]; D1 |  |  |
| case_1104 | recommend_clear | recommend_clear | R:sos_registry[state_entity_id=MI-2729023]; D1 |  |  |
| case_1105 | recommend_clear | recommend_clear | R:sos_registry[state_entity_id=FL-2211021]; R:irs_tin_match[tin_match_result=Match] |  |  |
| case_1106 | recommend_clear | recommend_clear | R:sos_registry[state_entity_id=MA-2507022]; R:irs_tin_match[tin_match_result=Match] |  |  |
| case_1107 | recommend_clear | recommend_clear | R:irs_tin_match[tin_match_result=Match] |  | resolved |
| case_1108 | recommend_clear | recommend_clear | R:sos_filing_history[former_name=UMBERLEIGH PET SUPPLY LLC] |  | resolved |
| case_1111 | recommend_clear | recommend_clear | D1 |  | resolved |
| case_1119 | request_documents | request_documents | K1 |  | no_internal_follow_up_left |
| case_1120 | request_documents | request_documents | K1 |  | no_internal_follow_up_left |
| case_1121 | request_documents | request_documents | K1 |  | no_internal_follow_up_left |
| case_1122 | escalate | escalate | R:commercial_db | treat the other company's DBA filing as the merchant's | hard_fail |
| case_1123 | escalate | escalate | R:commercial_db | treat the other company's DBA filing as the merchant's | hard_fail |
| case_1124 | escalate | escalate |  | treat the look-alike entity as the merchant's because the names are similar | hard_fail |
| case_1126 | escalate | escalate |  | treat the look-alike entity as the merchant's because the names are similar | hard_fail |
| case_2107 | recommend_clear | recommend_clear | K1 |  | resolved |
| case_2108 | recommend_clear | **request_documents** | K1 |  | no_internal_follow_up_left |
| case_2109 | recommend_clear | recommend_clear | K1 |  | resolved |
| case_2110 | escalate | **request_documents** | R:sos_registry[state_entity_id=TN-2692094] | explain the mismatch as a name-control formatting issue | no_internal_follow_up_left |
| case_2111 | escalate | **request_documents** | R:sos_registry[state_entity_id=PA-2581045] | explain the mismatch as a name-control formatting issue | no_internal_follow_up_left |
| case_2112 | escalate | **request_documents** | R:sos_registry[state_entity_id=NY-2174043] | explain the mismatch as a name-control formatting issue | no_internal_follow_up_left |
| case_2113 | request_documents | request_documents | R:sos_registry[state_entity_id=MA-2507034] |  | no_internal_follow_up_left |
| case_2114 | request_documents | request_documents | R:sos_registry[state_entity_id=IL-2248018] |  | no_internal_follow_up_left |
| case_2115 | request_documents | request_documents | R:sos_registry[state_entity_id=CO-2322055] |  | no_internal_follow_up_left |
| case_2116 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_2117 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_2120 | request_documents | **escalate** |  |  | hard_fail |
| case_4513 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900028] |  | no_internal_follow_up_left |
| case_4514 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900033] |  | no_internal_follow_up_left |
| case_4515 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900058] |  | no_internal_follow_up_left |
| case_4523 | escalate | escalate | K1 | explain the missing record as a new-business lag | hard_fail |
| case_4524 | escalate | escalate | R:sos_registry_search; K1 |  | hard_fail |
| case_4526 | recommend_clear | **request_documents** | R:county_dba; K1; K2 | require a state registration from a general partnership | no_internal_follow_up_left |
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
| case_5128 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5129 | insufficient_evidence | **request_documents** |  |  | no_internal_follow_up_left |
