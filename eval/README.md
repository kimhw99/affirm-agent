# affirm-eval

Evaluates this repo's KYB review agent against its golden dataset
(`../data/golden/*/*/{case,world,expected}.json`; `expected.json` is an `ExpectedAnswer`,
`contracts/kyb_contracts/golden.py`). Nothing outside `eval/` is modified. Set
`AFFIRM_AGENT_DIR` to evaluate another checkout.

## How it runs the agent

`run_eval.py` imports the agent service in-process and wires it the way
`services/agent/app/main.py` does: Lead LLM, real Records and Evidence workers,
LangFuse callbacks if the keys are set. The one substitution is the case service,
which is replaced by the agent's own `FakeCaseClient` over `data/golden`. That client
loads every `world.json` into shared tables, as the seeded database does, so look-alike
companies from other cases can be found. `expected.json` is never shown to the agent.

The graph pauses at step 10 (analyst decision). The evaluation reads the `ReviewOutput`
at that point and doesn't resume the graph.

## Setup and run

```bash
uv venv -p 3.12 .venv
uv pip install -p .venv -r requirements.txt
.venv/bin/python run_eval.py --judge                 # every golden case; reads OPENAI_API_KEY etc. from ../.env
.venv/bin/python run_eval.py --only ec2 --limit 5    # a slice
.venv/bin/python run_eval.py --lead-llm off          # deterministic Lead baseline
.venv/bin/python run_eval.py --today 2026-11-21      # pin the review date (compare runs on the same date)
.venv/bin/python run_eval.py --only case_2105 --dump # also save raw worker results + review output per case
.venv/bin/python report.py results/<run>             # rebuild report.md
```

Output goes to `results/<run>/`: `meta.json`, `results.jsonl` (one scored row per case,
with the agent's citations, flags and notes) and `report.md`.

## What is scored (`scoring.py`)

| Metric | Rule |
|---|---|
| Decision | `memo.recommendation` equals `expected_decision` (or an `acceptable_decisions` entry) |
| must_cite | Every item is cited in the memo's flag or ruled-out evidence. D/B/W/K are matched by ID. R records (`RecordRef`, `{source, where}`) match a cited R source with that `source_type` when, for each `where` field, one of its findings has that field and value (strings ignore case and extra spaces; a `filing_event` dict must be equal). `any_of` needs one. Each miss is tagged *retrieved* (a worker found it but the memo didn't cite it) or *never retrieved*. |
| forbidden_actions | Rules decide the decision-shaped ones (`recommend_clear`, `escalate …` when the agent didn't escalate, `resolve or comment on FLAG`). With `--judge`, an LLM judge (`judge.py`, `JUDGE_MODEL`, default `OPENAI_MODEL`) decides the rest from the case's `notes` and the memo. "put raw … text in shared_facts" is marked n/a, because the agent has no `shared_facts` structure. |
| Rounds | `internal_rounds` ≤ `max_follow_up_rounds` |
| Step-9 checks | The final memo passed the agent's own hard-rule checks |
| Strict pass | Correct decision, all must_cite items cited, and no forbidden-action violation |

Caveats: LLM runs aren't deterministic, so repeat runs vary by a few cases. Judge
verdicts are judgements, and the report labels them as `judge`.
