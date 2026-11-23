# affirm-agent golden-set evaluation

Run `pr1-ec1-combo` · review date 2026-11-23 · model `gpt-4o-mini` · Lead `OpenAILeadLLM` · workers LangGraphWorker, LangGraphWorker · judge on

## Headline

| Metric | Result |
|---|---|
| Cases run | 62 (0 crashed) |
| Decision accuracy | 50/62 (81%) |
| All must_cite items cited | 35/62 (56%) |
| must_cite recall (mean per case) | 73% |
| Forbidden-action violations | 5 of 68 checked (0 undecided) |
| Closed at intake with no worker run (auto_resolved only) | 5 |
| Step-9 checks passed | 62/62 (100%) |
| Within max_follow_up_rounds | 62/62 (100%) |
| **Strict pass** (decision + all citations + no violation) | **26/62 (42%)** |
| Seconds per case (mean / max) | 32 / 95 |

## By edge-case group

| Group | Cases | Decision | All citations | Cite recall | Strict |
|---|---|---|---|---|---|
| combo | 30 | 20/30 (67%) | 21/30 (70%) | 85% | 16/30 (53%) |
| ec1 | 29 | 27/29 (93%) | 11/29 (38%) | 58% | 7/29 (24%) |
| ec4 | 3 | 3/3 (100%) | 3/3 (100%) | 100% | 3/3 (100%) |

## Decision confusion matrix (rows = expected, columns = agent)

| expected \ got | recommend_clear | request_documents | escalate | insufficient_evidence | (none) |
|---|---|---|---|---|---|
| recommend_clear | 29 | 4 |  |  |  |
| request_documents |  | 15 |  | 2 |  |
| escalate |  | 5 | 6 |  |  |
| insufficient_evidence |  | 1 |  |  |  |

## must_cite misses

`retrieved` = a worker fetched a matching record but the memo didn't cite it; `never retrieved` = no worker looked it up.

| Item kind | Why missed | Count |
|---|---|---|
| `R:sos_registry` | never retrieved | 11 |
| `D1` | retrieved, not cited | 6 |
| `R:irs_tin_match` | never retrieved | 3 |
| `K1` | retrieved, not cited | 3 |
| `B1` | retrieved, not cited | 3 |
| `W1` | retrieved, not cited | 2 |
| `R:sos_filing_history` | never retrieved | 1 |
| `R:sos_filing_history` | retrieved, not cited | 1 |
| `R:commercial_db` | never retrieved | 1 |
| `R:commercial_db` | retrieved, not cited | 1 |
| `R:dba_search` | retrieved, not cited | 1 |

## Forbidden-action violations

| Action | Decided by | Cases |
|---|---|---|
| treat the expired filing as proof the DBA is current | judge | 2 |
| treat the look-alike entity as the merchant's because the names are similar | judge | 2 |
| treat the other company's DBA filing as the merchant's | judge | 1 |

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
| case_1109 | recommend_clear | recommend_clear | D1 |  | resolved |
| case_1110 | recommend_clear | recommend_clear | R:sos_filing_history[former_name=OLDCASTLE TEA COMPANY LLC] |  | resolved |
| case_1111 | recommend_clear | recommend_clear | D1 |  | resolved |
| case_1112 | recommend_clear | recommend_clear | D1 |  | resolved |
| case_1117 | request_documents | **insufficient_evidence** |  | treat the expired filing as proof the DBA is current | no_internal_follow_up_left |
| case_1118 | request_documents | **insufficient_evidence** |  | treat the expired filing as proof the DBA is current | no_internal_follow_up_left |
| case_1119 | request_documents | request_documents | K1 |  | no_internal_follow_up_left |
| case_1120 | request_documents | request_documents | K1 |  | no_internal_follow_up_left |
| case_1121 | request_documents | request_documents | K1 |  | no_internal_follow_up_left |
| case_1122 | escalate | escalate | R:commercial_db | treat the other company's DBA filing as the merchant's | hard_fail |
| case_1123 | escalate | escalate | R:commercial_db |  | hard_fail |
| case_1124 | escalate | escalate |  | treat the look-alike entity as the merchant's because the names are similar | hard_fail |
| case_1125 | escalate | escalate |  | treat the look-alike entity as the merchant's because the names are similar | hard_fail |
| case_1127 | request_documents | request_documents | W1 |  | no_internal_follow_up_left |
| case_1128 | request_documents | request_documents | W1 |  | no_internal_follow_up_left |
| case_5104 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5105 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5106 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5110 | escalate | **request_documents** | R:sos_registry[state_entity_id=OR-2396101] |  | no_internal_follow_up_left |
| case_5111 | escalate | **request_documents** | R:sos_registry[state_entity_id=MN-2618168] |  | no_internal_follow_up_left |
| case_5112 | escalate | **request_documents** | R:sos_registry[state_entity_id=TN-2692144] |  | no_internal_follow_up_left |
| case_5116 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900097]; R:dba_search |  | no_internal_follow_up_left |
| case_5117 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900108] |  | no_internal_follow_up_left |
| case_5118 | request_documents | request_documents | R:sos_registry[state_entity_id=DE-2900130] |  | no_internal_follow_up_left |
| case_5125 | escalate | **request_documents** | B1 |  | no_internal_follow_up_left |
| case_5126 | escalate | **request_documents** | B1 |  | no_internal_follow_up_left |
| case_5127 | escalate | escalate | B1 |  | hard_fail |
| case_5128 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
| case_5129 | insufficient_evidence | **request_documents** |  |  | no_internal_follow_up_left |
