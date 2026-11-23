# affirm-agent golden-set evaluation: results

**Run:** `results/full` · 2026-11-21 · 118 golden cases (`affirm-agent/data/golden`: ec1 29, ec2 30, ec4 30, combo 29)
**Agent setup:** Lead `OpenAILeadLLM` + real Records/Evidence workers (`LangGraphWorker`), all on `gpt-4o-mini`; review date 2026-11-21; LangFuse tracing on
**Scoring:** `scoring.py` rules + LLM judge (`judge.py`, `gpt-4o-mini`) for forbidden actions a rule can't decide
**Raw data:** `results/full/results.jsonl` (one scored row per case) · generated report: `results/full/report.md`

## Summary

The agent picks the expected decision in **66%** of cases. Only **39%** pass strictly: right decision, every required citation, and no forbidden action. The biggest weakness is **escalation: 2 of 17 escalate cases are right**. The agent's own step-9 hard-rule checks passed on all 118 memos, so they don't catch any of these errors.

## Headline metrics

| Metric                                                    | Result                                     |
| --------------------------------------------------------- | ------------------------------------------ |
| Cases run                                                 | 118 (0 crashed)                            |
| Decision accuracy                                         | 78/118 (66%)                               |
| All must_cite items cited                                 | 62/118 (53%)                               |
| must_cite recall (mean per case)                          | 73%                                        |
| Forbidden-action violations                               | 16 of 130 checked (5 by rule, 11 by judge) |
| Closed at intake with no worker run                       | 5                                          |
| Step-9 checks passed                                      | 118/118 (100%)                             |
| Within max_follow_up_rounds                               | 118/118 (100%)                             |
| **Strict pass** (decision + all citations + no violation) | **46/118 (39%)**                           |
| Seconds per case (mean / max)                             | 34 / 88                                    |

### By edge-case group

| Group | Cases | Decision | All citations | Cite recall | Strict |
|---|---|---|---|---|---|
| combo | 29 | 18/29 (62%) | 17/29 (59%) | 83% | 10/29 (34%) |
| ec1 | 29 | 19/29 (66%) | 12/29 (41%) | 61% | 6/29 (21%) |
| ec2 | 30 | 21/30 (70%) | 15/30 (50%) | 75% | 13/30 (43%) |
| ec4 | 30 | 20/30 (67%) | 18/30 (60%) | 73% | 15/30 (50%) |

### Decision confusion matrix (rows = expected, columns = agent)

| Expected \ agent | recommend_clear | request_documents | escalate | insufficient_evidence |
|---|---|---|---|---|
| recommend_clear (66) | **54** | 12 | – | – |
| request_documents (34) | 5 | **22** | 2 | 5 |
| escalate (17) | 3 | 12 | **2** | – |
| insufficient_evidence (1) | – | 1 | – | – |

## Findings

### 1. Under-escalation: 2 of 17 escalate cases correct

- **EIN held by an unrelated company** (case_2110–2112, case_5110–5112). The agent requests an IRS 147C letter instead of escalating. The Records worker never finds the company that holds the EIN. That company is in the registry (for example HOLLYHOCK JEWELRY LLC in case_2110), but no follow-up step searches for it. As a result, the `UNRELATED_EIN_MUST_ESCALATE` guardrail (`app/graph/guardrails.py`) can't fire, because it needs a separate entity that the TIN matches. The required `sos_registry` record was never retrieved in all six cases. In case_2110 and case_2111 the judge also found the memo explaining the mismatch as a name-control formatting issue, which is forbidden.
- **DBA registered to an unrelated company** (case_1122, case_1123, case_1125). The agent recommends clearing. It treats the other company's DBA filing as the merchant's (case_1123, judge) and never cites the look-alike's commercial profile.
- **Risk signals in owner KYC or bank evidence** (case_4522, case_4523, case_5125–5127). The agent asks for documents. K1 or B1, the evidence behind the escalation, isn't cited.
- **case_1126**: request_documents instead of escalate.

### 2. Over-requesting documents on clear cases (12 of 66)

- **Newly formed businesses with the EIN not yet issued** (case_5104–5106). `TIN_NOT_ISSUED` stays unresolved and leads to a 147C request. The golden answer treats a new EIN plus a matching CP575 and articles as enough to clear.
- **General partnerships** (case_4525–4527). The agent requests articles of organization, which means requiring a state registration from a general partnership. That's a forbidden action in all three (judge). D1, the county DBA, K1 and K2 are all uncited.
- **Others:** case_2103, case_2107, case_2108, case_2116, case_2117, case_5128.

### 3. Wrong call where documents are needed

- **Cleared when it should have requested documents:**
  - case_4505: an entity still pending (forbidden, rule).
  - case_1119–1121: officer-mismatch cases, cleared without citing owner KYC K1.
  - case_1113: cleared with no official link between the trade name and the entity (forbidden, rule).
- **`insufficient_evidence` instead of `request_documents`:**
  - case_1117, case_1118: an expired DBA. The judge flags "treat the expired filing as proof the DBA is current".
  - case_4513–4515: a Delaware registration that the agent never retrieves.
- **Escalated when documents would do:** case_2120 (TIN Not Found) and case_4528 (dissolution).

### 4. The memo drops evidence the workers found

| Item | Why missed | Count |
|---|---|---|
| `R:sos_registry` | never retrieved | 20 |
| `K1` | retrieved, not cited | 11 |
| `D1` | retrieved, not cited | 9 |
| `R:sos_filing_history` | never retrieved | 6 |
| `B1` | retrieved, not cited | 5 |
| `R:irs_tin_match` | never retrieved | 4 |
| `R:sos_filing_history` | retrieved, not cited | 4 |
| `R:irs_tin_match` | retrieved, not cited | 3 |
| `R:sos_registry` | retrieved, not cited | 3 |
| `R:county_dba` | retrieved, not cited | 3 |
| `K2` | retrieved, not cited | 3 |
| `R:commercial_db` | never retrieved | 2 |
| `W1`, `D2` | retrieved, not cited | 1 each |
| `R:sos_registry_search` | never retrieved | 1 |

Most D, B and K misses are **retrieved but not cited**: the workers found the evidence and the memo writer left it out. Most R misses are **never retrieved**, which means the workers' search strategy has gaps. In the EC-1 rename cases (case_1108–1110, case_5102, case_5103) the name change in the filing history is missing, even though it's the record that explains the flag.

### 5. Name-formatting flags closed with no evidence

In case_1102–1106, intake resolves `NAME_SIMILAR_MATCH` in code as a formatting difference. No worker runs and the memo cites nothing. The decision (recommend_clear) is right, but the golden set expects the registry record and D1 (or the TIN match) to back it up.

### 6. Step-9 checks don't catch these errors

Every final memo passed `CLEAR_NEEDS_TWO_SOURCES`, `UNRELATED_EIN_MUST_ESCALATE`, `NOT_ISSUED_TIN_CANT_CLEAR`, `NO_FULL_TAX_ID` and `CITED_ID_MUST_EXIST`. The rules hold for what the agent found. They can't catch a missing search (finding 1) or a wrong decision on correct evidence (findings 2 and 3).

### 7. The analyst notes are low quality

- Cleared cases still say "A GAP check is still open…" (for example case_1108, case_5122), which contradicts the recommendation.
- Worker filler appears word for word in the notes, for example "records worker, still unresolved: No unresolved questions.", "still unresolved: None", and "not found: holder".

### Forbidden-action violations

| Action | Decided by | Cases |
|---|---|---|
| require a state registration from a general partnership | judge | 3 (4525–4527) |
| treat the expired filing as proof the DBA is current | judge | 2 (1117, 1118) |
| recommend_clear | rule | 2 (1122, 1123) |
| explain the mismatch as a name-control formatting issue | judge | 2 (2110, 2111) |
| conclude the company is unregistered | judge | 2 (4520, 4521) |
| recommend_clear without an official link between the trade name and the entity | rule | 1 (1113) |
| treat the other company's DBA filing as the merchant's | judge | 1 (1123) |
| treat the look-alike entity as the merchant's because the names are similar | judge | 1 (1124) |
| recommend_clear while the entity is pending | rule | 1 (4505) |
| conclude the company does not exist | judge | 1 (5116) |

## Suggested next steps

1. **Add a follow-up step for TIN mismatches with no name change: search the registry for the company that holds the EIN.** Options include searching by the officers' names or the registered address, or a tool that tests candidate names against the TIN, so `UNRELATED_EIN_MUST_ESCALATE` can fire. This targets 6 escalate cases.
2. **When a DBA filing is registered to a different entity, conclude escalate, not clear** (case_1122, 1123, 1125).
3. **Clear new businesses with an unissued EIN when the CP575 and articles agree.** **Don't ask general partnerships or sole proprietors for state registration.**
4. **Make the memo writer cite K, D and B evidence the workers already found.** Consider a check that flags a resolved flag when relevant retrieved evidence is left uncited.
5. **Back intake's formatting-only auto-resolutions with the registry record**, or relax the golden expectation for those 5 cases.
6. **Clean up the analyst notes:** don't say a GAP is open on cleared cases, and filter worker filler.

## Method and caveats

- The agent runs in-process with the production wiring. The only substitution is the case service, which is replaced by the agent's `FakeCaseClient` over all golden folders, the same data the seed script loads. The graph is read at the analyst-decision pause and not resumed.
- R records are matched by `source_type` plus the fields given in `expected.yaml` against the workers' findings. `result` maps to `tin_match_result`, `event_type` to `filing_event.event_type`, and `state` and `name_tested` are matched against the record's locator.
- This is a single run of nondeterministic LLM calls, so expect a few cases to change between runs.
- 11 of the 16 violations are LLM-judge verdicts.
- "put raw document or website text in shared_facts" is in every case but is marked n/a, because the agent has no `shared_facts` structure.
- The first scoring pass counted the agent's code-written line "The agent did not review FLAG." as commenting on that flag. The rule was fixed and the run rescored, which removed 3 false violations. All numbers above are after that fix.

**Reproduce:** `cd affirm-eval && .venv/bin/python run_eval.py --judge --concurrency 8`. For a deterministic-Lead baseline, add `--lead-llm off`.
