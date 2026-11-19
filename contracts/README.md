# kyb-contracts

Shared data formats for the KYB exception review system. Whoever produces a format owns it; the services that use it review every change.

For a field-by-field walkthrough of `CaseInput` and the KYB background behind it, see [case_input_guide.md](case_input_guide.md).

| File | Defines | Used by |
|---|---|---|
| `kyb_contracts/case_input.py` | `CaseInput` and its parts | case service, intake, both workers |
| `kyb_contracts/decision.py` | `DecisionInput`, `DecisionRecord`, the status each decision leads to (`STATUS_AFTER`) and the override rule (`AGREES_WITH`, `is_override`) | review app, agent API, case service, evaluation |
| `kyb_contracts/worker_io.py` | Worker input and output (to add) | graph, workers |
| `kyb_contracts/memo.py` | Memo, the checks result, `evidence_index` (to add; `Recommendation` moves here from `decision.py`) | graph, review app |

## Rules

- **Structure only.** The contracts check types, formats and IDs, not business rules such as "an LLC must have a state ID". Missing or odd data is exactly what the agent has to handle, so the contract must be able to carry it.
- **No SSNs.** Owners appear as `owner:P1`. The case service keeps the real number in a separate table that the model never sees, and `CaseInput` rejects anything that looks like an SSN.
- **Merchant text is untrusted.** Document `text` and website `text` are read only by the Evidence worker.
- **Evidence IDs are fixed.** D (documents), B (bank), W (website) and K (KYC) IDs are set when the case arrives and never change.
- **Unknown fields are errors.** Every model uses `extra="forbid"`, so a typo fails loudly instead of being dropped.

## Use

```bash
pip install -e ./contracts            # in a service with uv: uv add --editable ../../contracts
```

After changing a contract, check from the repo root that the examples still validate:

```bash
python -c "import json, pathlib; from kyb_contracts import CaseInput; [CaseInput.model_validate(json.loads(p.read_text())) for p in pathlib.Path('contracts/examples/case_input').glob('*.json')]; print('ok')"
```

## Examples

All data in `examples/` is fictional. EINs start with `00`, which real EINs never do. Don't put SSNs in these files, not even fake ones starting with `000`: `CaseInput` rejects anything that looks like an SSN.

| File | Scenario |
|---|---|
| `case_1042.json` | EC-1 + EC-2: renamed in 2025; the IRS and the DBA record still show the old name |
| `case_2001.json` | EC-4: sole proprietor with no state registration; the TIN on file is the owner's SSN |
| `case_3001.json` | EC-2: the EIN belongs to an unrelated company |