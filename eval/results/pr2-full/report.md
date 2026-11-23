# affirm-agent golden-set evaluation

Run `pr2-full` · review date 2026-11-23 · model `gpt-4o-mini` · Lead `OpenAILeadLLM` · workers LangGraphWorker, LangGraphWorker · judge on

## Headline

| Metric | Result |
|---|---|
| Cases run | 119 (0 crashed) |
| Decision accuracy | 96/119 (81%) |
| All must_cite items cited | 67/119 (56%) |
| must_cite recall (mean per case) | 76% |
| Forbidden-action violations | 12 of 131 checked (0 undecided) |
| Closed at intake with no worker run (auto_resolved only) | 5 |
| Step-9 checks passed | 119/119 (100%) |
| Within max_follow_up_rounds | 119/119 (100%) |
| **Strict pass** (decision + all citations + no violation) | **54/119 (45%)** |
| Seconds per case (mean / max) | 33 / 63 |

## By edge-case group

| Group | Cases | Decision | All citations | Cite recall | Strict |
|---|---|---|---|---|---|
| combo | 30 | 19/30 (63%) | 21/30 (70%) | 87% | 16/30 (53%) |
| ec1 | 29 | 29/29 (100%) | 13/29 (45%) | 63% | 10/29 (34%) |
| ec2 | 30 | 22/30 (73%) | 14/30 (47%) | 73% | 10/30 (33%) |
| ec4 | 30 | 26/30 (87%) | 19/30 (63%) | 82% | 18/30 (60%) |

## Decision confusion matrix (rows = expected, columns = agent)

| expected \ got | recommend_clear | request_documents | escalate | insufficient_evidence | (none) |
|---|---|---|---|---|---|
| recommend_clear | 58 | 9 |  |  |  |
| request_documents |  | 32 | 2 |  |  |
| escalate |  | 11 | 6 |  |  |
| insufficient_evidence |  | 1 |  |  |  |

## must_cite misses

`retrieved` = a worker fetched a matching record but the memo didn't cite it; `never retrieved` = no worker looked it up.

| Item kind | Why missed | Count |
|---|---|---|
| `R:sos_registry` | never retrieved | 20 |
| `K1` | retrieved, not cited | 9 |
| `D1` | retrieved, not cited | 8 |
| `R:sos_registry` | retrieved, not cited | 4 |
| `B1` | retrieved, not cited | 4 |
| `R:irs_tin_match` | never retrieved | 3 |
| `R:sos_filing_history` | never retrieved | 3 |
| `R:county_dba` | retrieved, not cited | 3 |
| `R:irs_tin_match` | retrieved, not cited | 3 |
| `R:sos_filing_history` | retrieved, not cited | 2 |
| `R:commercial_db` | never retrieved | 2 |
| `W1` | retrieved, not cited | 1 |
| `D2` | retrieved, not cited | 1 |
| `R:county_dba` | never retrieved | 1 |
| `R:sos_registry_search` | never retrieved | 1 |
| `K2` | retrieved, not cited | 1 |

## Forbidden-action violations

| Action | Decided by | Cases |
|---|---|---|
| treat the look-alike entity as the merchant's because the names are similar | judge | 3 |
| explain the missing record as a new-business lag | judge | 3 |
| conclude the company does not exist | judge | 2 |
| require a state registration from a general partnership | judge | 2 |
| treat the other company's DBA filing as the merchant's | judge | 1 |
| explain the mismatch as a name-control formatting issue | judge | 1 |

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
| case_1109 | recommend_clear | recommend_clear | R:sos_filing_history[former_name=FALLOWMERE COFFEE ROASTERS INC] |  | resolved |
| case_1110 | recommend_clear | recommend_clear | R:sos_filing_history[former_name=OLDCASTLE TEA COMPANY LLC] |  | resolved |
| case_1111 | recommend_clear | recommend_clear | D1 |  | resolved |
| case_1119 | request_documents | request_documents | K1 |  | no_internal_follow_up_left |
| case_1120 | request_documents | request_documents | K1 |  | no_internal_follow_up_left |
| case_1121 | request_documents | request_documents | K1 |  | no_internal_follow_up_left |
| case_1122 | escalate | escalate | R:commercial_db | treat the other company's DBA filing as the merchant's | hard_fail |
| case_1123 | escalate | escalate | R:commercial_db |  | hard_fail |
| case_1124 | escalate | escalate |  | treat the look-alike entity as the merchant's because the names are similar | hard_fail |
| case_1125 | escalate | escalate |  | treat the look-alike entity as the merchant's because the names are similar | hard_fail |
| case_1126 | escalate | escalate |  | treat the look-alike entity as the merchant's because the names are similar | hard_fail |
| case_1128 | request_documents | request_documents | W1 |  | no_internal_follow_up_left |
| case_2102 | recommend_clear | recommend_clear | D2 |  | resolved |
| case_2103 | recommend_clear | **request_documents** |  |  | resolved |
| case_2107 | recommend_clear | **request_documents** | R:sos_registry[officer_name=Blake Ilunga]; K1 |  | no_internal_follow_up_left |
| case_2108 | recommend_clear | recommend_clear | K1 |  | resolved |
| case_2109 | recommend_clear | recommend_clear | K1 |  | resolved |
| case_2110 | escalate | **request_documents** | R:sos_registry[state_entity_id=TN-2692094] |  | no_internal_follow_up_left |
| case_2111 | escalate | **request_documents** | R:sos_registry[state_entity_id=PA-2581045] | explain the mismatch as a name-control formatting issue | no_internal_follow_up_left |
| case_2112 | escalate | **request_documents** | R:sos_registry[state_entity_id=NY-2174043] |  | no_internal_follow_up_left |
| case_2113 | request_documents | request_documents | R:sos_registry[state_entity_id=MA-2507034] |  | no_internal_follow_up_left |
| case_2114 | request_documents | request_documents | R:sos_registry[state_entity_id=IL-2248018] |  | resolved |
| case_2115 | request_documents | request_documents | R:sos_registry[state_entity_id=CO-2322055] |  | no_internal_follow_up_left |
| case_2116 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_2117 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_2120 | request_documents | **escalate** |  |  | hard_fail |
| case_2125 | recommend_clear | recommend_clear | R:county_dba |  | resolved |
| case_2126 | recommend_clear | recommend_clear | R:county_dba |  | resolved |
| case_2127 | recommend_clear | recommend_clear | R:county_dba |  | resolved |
| case_2128 | request_documents | request_documents | R:irs_tin_match[tin_match_result=Mismatch]; R:sos_registry[officer_name=Jonah Cordova] |  | no_internal_follow_up_left |
| case_2129 | request_documents | request_documents | R:irs_tin_match[tin_match_result=Mismatch]; R:sos_registry[officer_name=Uriel Xu] |  | no_internal_follow_up_left |
| case_2130 | request_documents | request_documents | R:irs_tin_match[tin_match_result=Mismatch]; R:sos_registry[officer_name=Talia Mbeki] |  | no_internal_follow_up_left |
| case_4513 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900028] |  | no_internal_follow_up_left |
| case_4514 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900033] | conclude the company does not exist | no_internal_follow_up_left |
| case_4515 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900058] |  | no_internal_follow_up_left |
| case_4522 | escalate | escalate | B1; D1 | explain the missing record as a new-business lag | hard_fail |
| case_4523 | escalate | **request_documents** | K1 | explain the missing record as a new-business lag | no_internal_follow_up_left |
| case_4524 | escalate | **request_documents** | R:sos_registry_search; K1 | explain the missing record as a new-business lag | no_internal_follow_up_left |
| case_4525 | recommend_clear | recommend_clear | D1 | require a state registration from a general partnership | resolved |
| case_4526 | recommend_clear | **request_documents** | D1; R:county_dba; K1; K2 | require a state registration from a general partnership | no_internal_follow_up_left |
| case_4527 | recommend_clear | recommend_clear | D1 |  | resolved |
| case_4528 | request_documents | **escalate** |  |  | hard_fail |
| case_4529 | recommend_clear | recommend_clear | R:sos_filing_history[filing_event={'event_id': 'MI-2729167-3', 'event_type': 'reinstatement', 'effective_date': '2026-01-16', 'old_value': 'dissolved', 'new_value': 'active'}] |  | resolved |
| case_4530 | recommend_clear | recommend_clear | R:sos_filing_history[filing_event={'event_id': 'NJ-2655126-3', 'event_type': 'reinstatement', 'effective_date': '2026-03-02', 'old_value': 'dissolved', 'new_value': 'active'}] |  | resolved |
| case_5104 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5105 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5106 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5110 | escalate | **request_documents** | R:sos_registry[state_entity_id=OR-2396101] |  | no_internal_follow_up_left |
| case_5111 | escalate | **request_documents** | R:sos_registry[state_entity_id=MN-2618168] |  | no_internal_follow_up_left |
| case_5112 | escalate | **request_documents** | R:sos_registry[state_entity_id=TN-2692144] |  | no_internal_follow_up_left |
| case_5116 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900097] | conclude the company does not exist | no_internal_follow_up_left |
| case_5117 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900108] |  | no_internal_follow_up_left |
| case_5118 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900130] |  | no_internal_follow_up_left |
| case_5125 | escalate | **request_documents** | B1 |  | no_internal_follow_up_left |
| case_5126 | escalate | **request_documents** | B1 |  | no_internal_follow_up_left |
| case_5127 | escalate | **request_documents** | B1 |  | no_internal_follow_up_left |
| case_5128 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5129 | insufficient_evidence | **request_documents** |  |  | no_internal_follow_up_left |
