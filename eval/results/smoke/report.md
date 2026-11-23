# affirm-agent golden-set evaluation

Run `smoke` · review date 2026-11-21 · model `gpt-4o-mini` · Lead `OpenAILeadLLM` · workers LangGraphWorker, LangGraphWorker · judge off

## Headline

| Metric | Result |
|---|---|
| Cases run | 3 (0 crashed) |
| Decision accuracy | 2/3 (67%) |
| All must_cite items cited | 2/3 (67%) |
| must_cite recall (mean per case) | 67% |
| Forbidden-action violations | 0 of 6 checked (4 undecided — run with --judge) |
| Step-9 checks passed | 3/3 (100%) |
| Within max_follow_up_rounds | 3/3 (100%) |
| **Strict pass** (decision + all citations + no violation) | **1/3 (33%)** |
| Seconds per case (mean / max) | 25 / 37 |

## By edge-case group

| Group | Cases | Decision | All citations | Cite recall | Strict |
|---|---|---|---|---|---|
| combo | 1 | 0/1 (0%) | 1/1 (100%) | 100% | 0/1 (0%) |
| ec1 | 1 | 1/1 (100%) | 0/1 (0%) | 0% | 0/1 (0%) |
| ec2 | 1 | 1/1 (100%) | 1/1 (100%) | 100% | 1/1 (100%) |

## Decision confusion matrix (rows = expected, columns = agent)

| expected \ got | recommend_clear | request_documents | escalate | insufficient_evidence | (none) |
|---|---|---|---|---|---|
| recommend_clear | 2 | 1 |  |  |  |

## must_cite misses

`retrieved` = a worker fetched a matching record but the memo didn't cite it; `never retrieved` = no worker looked it up.

| Item kind | Why missed | Count |
|---|---|---|
| `R:sos_registry` | never retrieved | 1 |
| `D1` | retrieved, not cited | 1 |

## Forbidden-action violations

None found.

## Cases that failed

| Case | Expected | Got | Missing citations | Violations | Stop reason |
|---|---|---|---|---|---|
| case_1102 | recommend_clear | recommend_clear | R:sos_registry[state_entity_id=NJ-2655017]; D1 |  |  |
| case_5105 | recommend_clear | **request_documents** |  |  | no_internal_follow_up_left |
