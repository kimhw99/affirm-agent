# affirm-agent golden-set evaluation: run 2 (after BEAC-30)

**Run:** `results/run2` · 2026-11-22 · same 118 golden cases (ec1 29, ec2 30, ec4 30, combo 29)
**Agent version:** `affirm-agent` @ `87ee8d3` (BEAC-30 merged)
- **ffd416e:** the Records worker covers every flagged playbook in its first run (checklist prompt and step reminders). `check_tin_match` returns the earlier result for a name with an already tested name control. `not_found` entries are pruned.
- **989258d:** tool-path recording. It doesn't change how a review behaves.

**Setup:** identical to run 1 (`experiment_results.md`): Lead `OpenAILeadLLM` + real workers on `gpt-4o-mini`, **review date pinned to 2026-11-21**, LangFuse on, same `scoring.py` rules (including the "did not review FLAG" fix), and the same LLM judge.
**Raw data:** `results/run2/results.jsonl` · `results/run2/report.md` · TIN-match follow-up check: `results/debug_tin/`

## Summary

**Decision quality didn't change meaningfully.** Decision accuracy went from 66% to 67% (+1 case). Strict pass went from 39% to 36% (−3), but that drop is a scoring artifact of the new `check_tin_match` behaviour. Counting an equivalent IRS Match record as cited, strict pass is **41%** (48/118; see finding 3). Escalation is still the main gap at **3 of 17** correct, up from 2.

**The update worked where it was aimed: worker efficiency and first-run coverage.**
- Cases needing a Lead follow-up fell from 78 to 58.
- Tool calls per case fell 12% and run time per case fell 9%.
- The name-change filing history is now retrieved and cited in all 5 rename cases where run 1 missed it.

## Headline metrics

| Metric | Run 1 | Run 2 | Δ |
|---|---|---|---|
| Cases run | 118 (0 crashed) | 118 (0 crashed) | – |
| Decision accuracy | 78/118 (66%) | 79/118 (67%) | +1 |
| All must_cite items cited | 62/118 (53%) | 61/118 (52%) | −1 |
| must_cite recall (mean per case) | 73% | 72% | −1 pt |
| Forbidden-action violations | 16 of 130 | 13 of 130 | −3 |
| Closed at intake with no worker run | 5 | 5 | – |
| Step-9 checks passed | 118/118 (100%) | 118/118 (100%) | – |
| Within max_follow_up_rounds | 118/118 (100%) | 118/118 (100%) | – |
| **Strict pass** | **46/118 (39%)** | **43/118 (36%)** | **−3** |
| Strict pass, TIN Match by name control (see finding 3) | 46/118 (39%) | 48/118 (41%) | +2 |
| Seconds per case (mean / max) | 34 / 88 | 31 / 57 | −9% / −35% |

### Efficiency (not in run 1's table)

| Metric | Run 1 | Run 2 | Δ |
|---|---|---|---|
| Cases with at least one Lead follow-up | 78 | 58 | −20 |
| Follow-up rounds per case (mean) | 0.90 | 0.69 | −23% |
| Worker runs per case (mean) | 3.57 | 3.19 | −11% |
| Tool calls per case (mean) | 10.98 | 9.65 | −12% |
| Worker errors | 0 | 0 | – |

### By edge-case group

| Group | Decision (1 → 2) | All citations (1 → 2) | Cite recall (1 → 2) | Strict (1 → 2) |
|---|---|---|---|---|
| combo (29) | 18 → 19 | 17 → **20** | 83% → 86% | 10 → **14** |
| ec1 (29) | 19 → 19 | 12 → 11 | 61% → 58% | 6 → 6 |
| ec2 (30) | 21 → 21 | 15 → **12** | 75% → 70% | 13 → **8** |
| ec4 (30) | 20 → 20 | 18 → 18 | 73% → 73% | 15 → 15 |

The ec2 drop is almost all finding 3. The combo gain is mostly the filing-history fix (finding 2).

### Decision confusion matrix, run 2 (run 1 in brackets)

| Expected \ agent | recommend_clear | request_documents | escalate | insufficient_evidence |
|---|---|---|---|---|
| recommend_clear (66) | **54** [54] | 12 [12] | – | – |
| request_documents (34) | 5 [5] | **22** [22] | 2 [2] | 5 [5] |
| escalate (17) | 4 [3] | 10 [12] | **3** [2] | – |
| insufficient_evidence (1) | – | 1 [1] | – | – |

Only 4 cases changed decision, and all are escalate cases:

| Case | Expected | Run 1 | Run 2 | |
|---|---|---|---|---|
| case_4522 | escalate | request_documents | **escalate** | fixed |
| case_5127 | escalate | request_documents | **escalate** | fixed |
| case_4524 | escalate | escalate | request_documents | regressed |
| case_1126 | escalate | request_documents | **recommend_clear** | worse: cleared a merchant whose entity ID belongs to another, inactive company |

With one run each, a ±1–2 case swing in a 17-case bucket is within LLM run-to-run noise.

## Findings

### 1. Escalation is still the main gap: 3 of 17 correct, up from 2

Run 1's escalation findings all still hold:
- **EIN held by an unrelated company** (case_2110–2112, case_5110–5112): all 6 still get request_documents. The first-run checklist doesn't include searching the registry for the company that holds the EIN, so `UNRELATED_EIN_MUST_ESCALATE` still can't fire. The required `sos_registry` record is never retrieved in any of them. In case_2110 and case_2111 the judge again finds the mismatch explained as a name-control formatting issue (forbidden).
- **DBA or entity ID belonging to another company** (case_1122, 1123, 1125, and now 1126): all four get recommend_clear. That's the riskiest error in the set, and it went from 3 cases to 4.
- **KYC or bank risk signals** (case_4523, case_4524, case_5125, case_5126): request_documents, with K1 and B1 uncited. case_4522 and case_5127 now escalate correctly.

### 2. Fixed: the name-change filing history is now retrieved

`R:sos_filing_history` "never retrieved" fell from 6 to 3. case_2103, case_5102, case_5103 and case_5122 now cite the name-change filing. case_5102, case_5103 and case_5122 became strict passes. The 3 left are case_4528–4530, which need dissolution and reinstatement events, not name changes.

### 3. New scoring artifact: the TIN-match result is reused under the first name tested

`R:irs_tin_match` "never retrieved" rose from 4 to 9. That drives the drops in all-citations and strict pass, and the ec2 drop.

The cause is the new rule in `check_tin_match`. A name with the same IRS name control as one already tested returns the **earlier** R source, recorded under the earlier name. In these cases the applied legal name already has the same name control as the IRS name. For example, "Whitlow & Underwood Bakery Inc" and "WHITLOW AND UNDERWOOD BAKERY INC" are both `WHIT`. So the record the agent cites is a Match under the applied name, while `expected.yaml` asks for a Match under the IRS name (`name_tested: …`).

A re-run of the affected cases with raw worker output saved (`--dump`, `results/debug_tin/`) confirmed this:

| Case | Record cited | Name tested | Name control | Result |
|---|---|---|---|---|
| case_2105 | R3 | Whitlow & Underwood Bakery Inc | WHIT | Match |
| case_2125 | R2 | Bluefen Outdoor Gear LLC | BLUE | Match |
| case_2126 | R3 | Keystone Tile and Stone Inc | KEYS | Match |
| case_5115 | R3 | Mulberry Sleep | MULB | Match |

The IRS compares only the name control, so this is equivalent evidence. 5 cases fail strict pass **only** because of it: case_2105, 2106, 2125, 2127 and 5115. Counting them, strict pass is 48/118 (41%) and all-citations is 66/118 (56%).

This also exposes a golden-set inconsistency. In these cases the simulated IRS check returns Match on the applied name, yet the vendor raised `TIN_MISMATCH`. Either the golden `must_cite` should accept any Match record with the IRS name's name control, or the scorer should match `name_tested` by name control. Pick one before comparing future runs.

**A real miss in the same group:** case_1107 now skips the TIN check entirely. It's a hyphen-only name case, where run 1 retrieved the record but didn't cite it.

### 4. Unchanged: wrong calls where documents are needed

All of these are the same cases as run 1:
- **Cleared when documents were needed:** case_1113 (no official link between the trade name and the entity, forbidden), case_1119–1121 (officer mismatch, K1 uncited), case_4505 (pending entity, forbidden).
- **`insufficient_evidence` instead of request_documents:** case_1117, case_1118 (expired DBA) and case_4513–4515 (Delaware registration never retrieved).
- **Escalated when documents would do:** case_2120 and case_4528.

### 5. Unchanged: documents requested on cases that should clear

There are 12 such cases, the same ones as run 1:
- New businesses with an unissued EIN (case_5104–5106).
- General partnerships asked for a state registration (case_4525–4527, forbidden).
- case_2103, 2107, 2108, 2116, 2117 and 5128.

### 6. Citation misses: workers fetch evidence the memo leaves out

| Item | Why missed | Run 1 | Run 2 |
|---|---|---|---|
| `R:sos_registry` | never retrieved | 20 | 20 |
| `K1` | retrieved, not cited | 11 | 11 |
| `R:irs_tin_match` | never retrieved | 4 | **9** (finding 3) |
| `D1` | retrieved, not cited | 9 | 8 |
| `B1` | retrieved, not cited | 5 | 5 |
| `R:sos_registry` | retrieved, not cited | 3 | 4 |
| `R:sos_filing_history` | retrieved, not cited | 4 | 3 |
| `R:sos_filing_history` | never retrieved | 6 | **3** (finding 2) |
| `K2` | retrieved, not cited | 3 | 3 |
| `R:irs_tin_match` | retrieved, not cited | 3 | 2 |
| `R:county_dba` | retrieved, not cited | 3 | 2 |
| `R:commercial_db` | never retrieved | 2 | 2 |
| `W1` | retrieved, not cited | 1 | 2 |
| `D2` | retrieved, not cited | 1 | 1 |
| `R:county_dba` | never retrieved | 0 | 1 |
| `R:sos_registry_search` | never retrieved / retrieved, not cited | 1 / 0 | 1 / 1 |

The memo writer still drops K, D and B evidence the workers found, at about the same rate. The update targeted the workers, not the memo writer.

### 7. Forbidden-action violations: 16 → 13

| Action | Decided by | Run 1 | Run 2 |
|---|---|---|---|
| require a state registration from a general partnership | judge | 3 | 3 (4525–4527) |
| recommend_clear | rule | 2 | 2 (1122, 1123) |
| explain the mismatch as a name-control formatting issue | judge | 2 | 2 (2110, 2111) |
| conclude the company is unregistered | judge | 2 (4520, 4521) | 2 (4519, 4521) |
| treat the expired filing as proof the DBA is current | judge | 2 | 1 (1118) |
| recommend_clear without an official link between the trade name and the entity | rule | 1 | 1 (1113) |
| recommend_clear while the entity is pending | rule | 1 | 1 (4505) |
| conclude the company does not exist | judge | 1 (5116) | 1 (5117) |
| treat the other company's DBA filing as the merchant's | judge | 1 | 0 |
| treat the look-alike entity as the merchant's because the names are similar | judge | 1 | 0 |

Five violations went away (case_1117, 1123, 1124, 4520, 5116) and two new ones appeared (case_4519, 5117). Most of these are judge verdicts on sibling cases with the same story, so treat the −3 as noise, not as a fix.

### 8. Unchanged: memo and intake issues

- **Intake still closes formatting-only name flags with no evidence:** case_1102–1106.
- **The agent's own checks still pass everything:** step-9 hard-rule checks pass on 118/118 memos while 39 decisions are wrong.
- **Analyst notes still contradict cleared decisions** ("A GAP or CONFLICT check remains open…" on case_1108, 2105 and 5122).

## What to do next

1. **Settle finding 3 before the next comparison.** Either relax `must_cite` to accept any Match record with the IRS name's name control, or have the scorer compare `name_tested` by name control. Otherwise TIN cases will keep showing false regressions.
2. **Escalation is still the highest-value fix (14 of 17 wrong):**
   - Add an EIN-holder registry search to the Records worker's checklist (6 cases).
   - Escalate when the DBA or entity ID belongs to another company (4 cases, all currently recommend_clear).
3. **Give the memo writer the same treatment the workers just got:** a check that a resolved flag cites the K, D and B evidence the workers retrieved.
4. **The remaining run-1 next steps still apply:** clear new businesses with an unissued EIN, don't require state registration from partnerships or sole proprietors, and clean up the analyst notes.
5. **Run each version 2–3 times to measure noise.** One run each can't separate a 1–3 case change from noise.

## Method and caveats

- Same harness, scorer and judge as run 1. The review date is pinned (`--today 2026-11-21`) so date-relative logic (new-business age) behaves the same way.
- One run per version. LLM nondeterminism alone moves a few cases between runs, so differences of 1–3 cases aren't significant.
- 9 of the 13 violations are LLM-judge verdicts.
- `run_eval.py` gained a `--dump` flag that saves raw worker results and the review output per case. It was used only for the `results/debug_tin` check, not for the main run.

**Reproduce:** `cd affirm-eval && .venv/bin/python run_eval.py --judge --concurrency 8 --today 2026-11-21`
