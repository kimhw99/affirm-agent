# affirm-agent golden-set evaluation

Run `debug_tin` · review date 2026-11-21 · model `gpt-4o-mini` · Lead `OpenAILeadLLM` · workers LangGraphWorker, LangGraphWorker · judge off

## Headline

| Metric | Result |
|---|---|
| Cases run | 5 (0 crashed) |
| Decision accuracy | 5/5 (100%) |
| All must_cite items cited | 0/5 (0%) |
| must_cite recall (mean per case) | 47% |
| Forbidden-action violations | 0 of 5 checked (0 undecided) |
| Closed at intake with no worker run (auto_resolved only) | 0 |
| Step-9 checks passed | 5/5 (100%) |
| Within max_follow_up_rounds | 5/5 (100%) |
| **Strict pass** (decision + all citations + no violation) | **0/5 (0%)** |
| Seconds per case (mean / max) | 25 / 28 |

## By edge-case group

| Group | Cases | Decision | All citations | Cite recall | Strict |
|---|---|---|---|---|---|
| combo | 1 | 1/1 (100%) | 0/1 (0%) | 67% | 0/1 (0%) |
| ec1 | 1 | 1/1 (100%) | 0/1 (0%) | 50% | 0/1 (0%) |
| ec2 | 3 | 3/3 (100%) | 0/3 (0%) | 39% | 0/3 (0%) |

## Decision confusion matrix (rows = expected, columns = agent)

| expected \ got | recommend_clear | request_documents | escalate | insufficient_evidence | (none) |
|---|---|---|---|---|---|
| recommend_clear | 5 |  |  |  |  |

## must_cite misses

`retrieved` = a worker fetched a matching record but the memo didn't cite it; `never retrieved` = no worker looked it up.

| Item kind | Why missed | Count |
|---|---|---|
| `R:irs_tin_match` | never retrieved | 5 |
| `R:county_dba` | never retrieved | 1 |
| `R:county_dba` | retrieved, not cited | 1 |

## Forbidden-action violations

None found.

## Cases that failed

| Case | Expected | Got | Missing citations | Violations | Stop reason |
|---|---|---|---|---|---|
| case_1107 | recommend_clear | recommend_clear | R:irs_tin_match[result=Match] |  | resolved |
| case_2105 | recommend_clear | recommend_clear | R:irs_tin_match[name_tested=WHITLOW AND UNDERWOOD BAKERY INC, result=Match] |  | resolved |
| case_2125 | recommend_clear | recommend_clear | R:county_dba; R:irs_tin_match[name_tested=BLUEFEN OUTFITTERS, result=Match] |  | resolved |
| case_2126 | recommend_clear | recommend_clear | R:county_dba; R:irs_tin_match[name_tested=KEYSTONE TILE STUDIO, result=Match] |  | resolved |
| case_5115 | recommend_clear | recommend_clear | R:irs_tin_match[name_tested=MULBERRY MATTRESS LLC, result=Match] |  | resolved |
