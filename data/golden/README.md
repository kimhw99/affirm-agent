# Golden cases

The test cases for the whole system. Each case holds the case the existing KYB system hands over and the official and vendor records the agent can look up. Everything is made up: no tool in this project calls a real registry, the IRS or a data vendor.

The case service's seed script ([seed.py](../../services/case/seed.py)) loads both files of every case into its database, the agent's offline tests read the same files through a fake client, and the evaluation compares the agent's results with each case's expected answer.

## Layout

```text
data/golden/
├── ec1/      EC-1: legal name vs DBA, suffix and punctuation variants
├── ec2/      EC-2: EIN/TIN mismatch, including IRS name-control issues
├── ec4/      EC-4: newly formed businesses and sole proprietors missing from databases
└── combo/    cases that combine edge cases
    └── case_1042/      one folder per case, named after its case_id
        ├── case.json
        └── world.json
```

Proposed split (to confirm; `docs/workflow-handoffs.md`, section 4): XL writes EC-1, E writes EC-2, H writes EC-4 and the combined cases.

## The two files

| File | What it stands for | Model | Example |
|---|---|---|---|
| `case.json` | The case the existing system hands over: application, vendor checks, extracted documents, bank verification, website fields, owner KYC | `CaseInput` in [case_input.py](../../contracts/kyb_contracts/case_input.py) | [case_1042](../../contracts/examples/case_input/case_1042.json) |
| `world.json` | What the Records worker's tools can find: registry entities with their filing history, DBA filings, commercial database profiles, and the IRS record for the submitted TIN | `World` in [world.py](../../contracts/kyb_contracts/world.py) | [case_1042](../../contracts/examples/world/case_1042.json) |

What each `case.json` field means: [case_input_guide.md](../../contracts/case_input_guide.md). How each `world.json` record is served: [case-service-endpoints.md](../../docs/case-service-endpoints.md).

## Writing a case

1. Start from the story: which flags the existing system raised, what really happened, and what the agent should conclude.
2. Write `case.json`: the flags, the vendor `checks` behind them, and the merchant's evidence.
3. Write `world.json` so the records tell that story. Include everything the agent should find, including records that point the wrong way, such as an unrelated company that holds the EIN, or a look-alike name. Leave out what shouldn't be found: a sole proprietor has no registry entity.
4. Run `python scripts/validate_golden.py` and fix what it reports.

For a hidden-instructions case, put the instruction-like text where a merchant could have written it, such as a website snapshot's extracted fields, and store it as it is.

## Rules

- **No full tax IDs.** A TIN appears only as its type and last 4 digits, and owners as `owner:P1`. `CaseInput` and `World` reject anything that looks like a full SSN or EIN, fake or not.
- **Made-up companies, people and addresses.** Website URLs end in `.example`.
- **IDs unique across all cases.** Every `world.json` goes into the same tables, so a `state_entity_id`, filing `event_id`, DBA `filing_id` or commercial `profile_id` can't appear in two cases. Avoid look-alike company names across cases too, unless you mean them: the registry search would find both.
- **The two files agree.** `scripts/validate_golden.py` checks that:
  - the folder name and both files have the same `case_id`
  - `case.json` (`tin_type`, `tin_last4`) and `world.json` (`tin_record`) describe the same TIN, or neither has one
  - `checks.tin` fits the IRS status: Match or Mismatch needs `issued`, Not Found needs `not_found`, Not Issued needs `not_issued`
  - `checks.sos_name.matched_name`, when given, is the `legal_name` of a registry entity in `world.json`
  - each flag has the vendor result behind it, e.g. `TIN_MISMATCH` needs `checks.tin` = Mismatch
  - a case in `ec1`, `ec2` or `ec4` has a flag of that edge case, and a case in `combo` has flags from at least two

## Checking

From the repo root (needs pydantic; `pip install -e ./contracts` installs it):

```bash
python scripts/validate_golden.py                    # every case
python scripts/validate_golden.py data/golden/ec1    # one folder
```

It lists each case as `ok` or with its problems, and exits with 1 if there are any. The checks across cases see only the cases in that run, so run it with no argument before committing.

## Not decided yet

- **The expected-answer file**: its name and format. The draft is the YAML in `docs/workflow-handoffs.md` 7.18: expected_decision (a `Recommendation` from [memo.py](../../contracts/kyb_contracts/memo.py)), must_cite and forbidden_actions. It will sit next to the two files, and the seed script won't load it, so the agent never sees it. Keep answers out of `case.json` and `world.json`.
- **How `NAME_UNVERIFIED` and `ENTITY_NOT_FOUND` differ**: open question 10 in `docs/workflow-handoffs.md`.
