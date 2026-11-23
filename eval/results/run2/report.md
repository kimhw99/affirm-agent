# affirm-agent golden-set evaluation

Run `run2` · review date 2026-11-21 · model `gpt-4o-mini` · Lead `OpenAILeadLLM` · workers LangGraphWorker, LangGraphWorker · judge on

## Headline

| Metric | Result |
|---|---|
| Cases run | 118 (0 crashed) |
| Decision accuracy | 79/118 (67%) |
| All must_cite items cited | 61/118 (52%) |
| must_cite recall (mean per case) | 72% |
| Forbidden-action violations | 13 of 130 checked (0 undecided) |
| Closed at intake with no worker run (auto_resolved only) | 5 |
| Step-9 checks passed | 118/118 (100%) |
| Within max_follow_up_rounds | 118/118 (100%) |
| **Strict pass** (decision + all citations + no violation) | **43/118 (36%)** |
| Seconds per case (mean / max) | 31 / 57 |

## By edge-case group

| Group | Cases | Decision | All citations | Cite recall | Strict |
|---|---|---|---|---|---|
| combo | 29 | 19/29 (66%) | 20/29 (69%) | 86% | 14/29 (48%) |
| ec1 | 29 | 19/29 (66%) | 11/29 (38%) | 58% | 6/29 (21%) |
| ec2 | 30 | 21/30 (70%) | 12/30 (40%) | 70% | 8/30 (27%) |
| ec4 | 30 | 20/30 (67%) | 18/30 (60%) | 73% | 15/30 (50%) |

## Decision confusion matrix (rows = expected, columns = agent)

| expected \ got | recommend_clear | request_documents | escalate | insufficient_evidence | (none) |
|---|---|---|---|---|---|
| recommend_clear | 54 | 12 |  |  |  |
| request_documents | 5 | 22 | 2 | 5 |  |
| escalate | 4 | 10 | 3 |  |  |
| insufficient_evidence |  | 1 |  |  |  |

## must_cite misses

`retrieved` = a worker fetched a matching record but the memo didn't cite it; `never retrieved` = no worker looked it up.

| Item kind | Why missed | Count |
|---|---|---|
| `R:sos_registry` | never retrieved | 20 |
| `K1` | retrieved, not cited | 11 |
| `R:irs_tin_match` | never retrieved | 9 |
| `D1` | retrieved, not cited | 8 |
| `B1` | retrieved, not cited | 5 |
| `R:sos_registry` | retrieved, not cited | 4 |
| `R:sos_filing_history` | retrieved, not cited | 3 |
| `K2` | retrieved, not cited | 3 |
| `R:sos_filing_history` | never retrieved | 3 |
| `R:commercial_db` | never retrieved | 2 |
| `W1` | retrieved, not cited | 2 |
| `R:irs_tin_match` | retrieved, not cited | 2 |
| `R:county_dba` | retrieved, not cited | 2 |
| `D2` | retrieved, not cited | 1 |
| `R:county_dba` | never retrieved | 1 |
| `R:sos_registry_search` | retrieved, not cited | 1 |
| `R:sos_registry_search` | never retrieved | 1 |

## Forbidden-action violations

| Action | Decided by | Cases |
|---|---|---|
| require a state registration from a general partnership | judge | 3 |
| recommend_clear | rule | 2 |
| explain the mismatch as a name-control formatting issue | judge | 2 |
| conclude the company is unregistered | judge | 2 |
| recommend_clear without an official link between the trade name and the entity | rule | 1 |
| treat the expired filing as proof the DBA is current | judge | 1 |
| recommend_clear while the entity is pending | rule | 1 |
| conclude the company does not exist | judge | 1 |

## Cases that failed

| Case | Expected | Got | Missing citations | Violations | Stop reason |
|---|---|---|---|---|---|
| case_1102 | recommend_clear | recommend_clear | R:sos_registry[state_entity_id=NJ-2655017]; D1 |  |  |
| case_1103 | recommend_clear | recommend_clear | R:sos_registry[state_entity_id=OH-2544040]; D1 |  |  |
| case_1104 | recommend_clear | recommend_clear | R:sos_registry[state_entity_id=MI-2729023]; D1 |  |  |
| case_1105 | recommend_clear | recommend_clear | R:sos_registry[state_entity_id=FL-2211021]; R:irs_tin_match[result=Match] |  |  |
| case_1106 | recommend_clear | recommend_clear | R:sos_registry[state_entity_id=MA-2507022]; R:irs_tin_match[result=Match] |  |  |
| case_1107 | recommend_clear | recommend_clear | R:irs_tin_match[result=Match] |  | resolved |
| case_1108 | recommend_clear | recommend_clear | R:sos_filing_history[event_type=name_change] |  | resolved |
| case_1109 | recommend_clear | recommend_clear | R:sos_filing_history[event_type=name_change] |  | resolved |
| case_1110 | recommend_clear | recommend_clear | R:sos_filing_history[event_type=name_change] |  | resolved |
| case_1111 | recommend_clear | recommend_clear | D1 |  | resolved |
| case_1112 | recommend_clear | recommend_clear | D1 |  | resolved |
| case_1113 | request_documents | **recommend_clear** |  | recommend_clear without an official link between the trade name and the entity | resolved |
| case_1117 | request_documents | **insufficient_evidence** |  |  | no_internal_follow_up_left |
| case_1118 | request_documents | **insufficient_evidence** |  | treat the expired filing as proof the DBA is current | no_internal_follow_up_left |
| case_1119 | request_documents | **recommend_clear** | K1 |  | resolved |
| case_1120 | request_documents | **recommend_clear** | K1 |  | resolved |
| case_1121 | request_documents | **recommend_clear** | K1 |  | resolved |
| case_1122 | escalate | **recommend_clear** | R:commercial_db | recommend_clear | resolved |
| case_1123 | escalate | **recommend_clear** | R:commercial_db | recommend_clear | resolved |
| case_1125 | escalate | **recommend_clear** |  |  | resolved |
| case_1126 | escalate | **recommend_clear** |  |  | resolved |
| case_1127 | request_documents | request_documents | W1 |  | no_internal_follow_up_left |
| case_1128 | request_documents | request_documents | W1 |  | no_internal_follow_up_left |
| case_2102 | recommend_clear | recommend_clear | D2 |  | resolved |
| case_2103 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_2105 | recommend_clear | recommend_clear | R:irs_tin_match[name_tested=WHITLOW AND UNDERWOOD BAKERY INC, result=Match] |  | resolved |
| case_2106 | recommend_clear | recommend_clear | R:irs_tin_match[name_tested=NETHERBY CAMERA SUPPLY LIMITED LIABILITY COMPANY, result=Match] |  | resolved |
| case_2107 | recommend_clear | **request_documents** | K1 |  | no_internal_follow_up_left |
| case_2108 | recommend_clear | **request_documents** | K1 |  | no_internal_follow_up_left |
| case_2109 | recommend_clear | recommend_clear | R:sos_registry[officer_name=Rafael Yilmaz]; K1 |  | resolved |
| case_2110 | escalate | **request_documents** | R:sos_registry[state_entity_id=TN-2692094] | explain the mismatch as a name-control formatting issue | no_internal_follow_up_left |
| case_2111 | escalate | **request_documents** | R:sos_registry[state_entity_id=PA-2581045] | explain the mismatch as a name-control formatting issue | no_internal_follow_up_left |
| case_2112 | escalate | **request_documents** | R:sos_registry[state_entity_id=NY-2174043] |  | no_internal_follow_up_left |
| case_2113 | request_documents | request_documents | R:sos_registry[state_entity_id=MA-2507034] |  | no_internal_follow_up_left |
| case_2114 | request_documents | request_documents | R:sos_registry[state_entity_id=IL-2248018] |  | no_internal_follow_up_left |
| case_2115 | request_documents | request_documents | R:sos_registry[state_entity_id=CO-2322055] |  | no_internal_follow_up_left |
| case_2116 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_2117 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_2120 | request_documents | **escalate** |  |  | hard_fail |
| case_2125 | recommend_clear | recommend_clear | R:irs_tin_match[name_tested=BLUEFEN OUTFITTERS, result=Match] |  | resolved |
| case_2126 | recommend_clear | recommend_clear | R:county_dba; R:irs_tin_match[name_tested=KEYSTONE TILE STUDIO, result=Match] |  | resolved |
| case_2127 | recommend_clear | recommend_clear | R:irs_tin_match[name_tested=QUARRYSTONE PETS, result=Match] |  | resolved |
| case_2128 | request_documents | request_documents | R:sos_registry[officer_name=Jonah Cordova] |  | no_internal_follow_up_left |
| case_2129 | request_documents | request_documents | R:irs_tin_match[result=Mismatch]; R:sos_registry[officer_name=Uriel Xu] |  | no_internal_follow_up_left |
| case_2130 | request_documents | request_documents | R:irs_tin_match[result=Mismatch]; R:sos_registry[officer_name=Talia Mbeki] |  | no_internal_follow_up_left |
| case_4505 | request_documents | **recommend_clear** |  | recommend_clear while the entity is pending | resolved |
| case_4513 | request_documents | **insufficient_evidence** | R:sos_registry[state=DE] |  | no_internal_follow_up_left |
| case_4514 | request_documents | **insufficient_evidence** | R:sos_registry[state=DE] |  | no_internal_follow_up_left |
| case_4515 | request_documents | **insufficient_evidence** | R:sos_registry[state=DE] |  | no_internal_follow_up_left |
| case_4519 | recommend_clear | recommend_clear |  | conclude the company is unregistered | resolved |
| case_4521 | recommend_clear | recommend_clear |  | conclude the company is unregistered | resolved |
| case_4522 | escalate | escalate | R:sos_registry_search; B1 |  | hard_fail |
| case_4523 | escalate | **request_documents** | K1; B1 |  | no_internal_follow_up_left |
| case_4524 | escalate | **request_documents** | R:sos_registry_search; K1; B1 |  | no_internal_follow_up_left |
| case_4525 | recommend_clear | **request_documents** | D1; R:county_dba; K1; K2 | require a state registration from a general partnership | no_internal_follow_up_left |
| case_4526 | recommend_clear | **request_documents** | D1; R:county_dba; K1; K2 | require a state registration from a general partnership | no_internal_follow_up_left |
| case_4527 | recommend_clear | **request_documents** | D1; K1; K2 | require a state registration from a general partnership | no_internal_follow_up_left |
| case_4528 | request_documents | **escalate** | R:sos_filing_history[event_type=dissolution] |  | hard_fail |
| case_4529 | recommend_clear | recommend_clear | R:sos_filing_history[event_type=reinstatement] |  | resolved |
| case_4530 | recommend_clear | recommend_clear | R:sos_filing_history[event_type=reinstatement] |  | resolved |
| case_5104 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5105 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5106 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5110 | escalate | **request_documents** | R:sos_registry[state_entity_id=OR-2396101] |  | no_internal_follow_up_left |
| case_5111 | escalate | **request_documents** | R:sos_registry[state_entity_id=MN-2618168] |  | no_internal_follow_up_left |
| case_5112 | escalate | **request_documents** | R:sos_registry[state_entity_id=TN-2692144] |  | no_internal_follow_up_left |
| case_5115 | recommend_clear | recommend_clear | R:irs_tin_match[name_tested=MULBERRY MATTRESS LLC, result=Match] |  | resolved |
| case_5116 | request_documents | request_documents | R:sos_registry[state=DE] |  | no_internal_follow_up_left |
| case_5117 | request_documents | request_documents | R:sos_registry[state=DE] | conclude the company does not exist | no_internal_follow_up_left |
| case_5118 | request_documents | request_documents | R:sos_registry[state=DE] |  | no_internal_follow_up_left |
| case_5125 | escalate | **request_documents** | B1 |  | no_internal_follow_up_left |
| case_5126 | escalate | **request_documents** |  |  | no_internal_follow_up_left |
| case_5127 | escalate | escalate | B1 |  | hard_fail |
| case_5128 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5129 | insufficient_evidence | **request_documents** |  |  | no_internal_follow_up_left |
