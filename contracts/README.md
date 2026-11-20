# kyb-contracts

Shared data formats for the KYB exception review system. Whoever produces a format owns it; the services that use it review every change.

For a field-by-field walkthrough of `CaseInput` and the KYB background behind it, see [case_input_guide.md](case_input_guide.md).

| File | Defines | Used by |
|---|---|---|
| `kyb_contracts/case_input.py` | `CaseInput` and its parts | case service, intake, both workers |
| `kyb_contracts/world.py` | `World`: a golden case's `world.json`, the mocked registry, DBA, commercial and IRS records. Also what case-service endpoints 1–6 return (`RegistryEntity`, `Filing`, `DbaFiling`, `CommercialProfile`, `TinRecord`) | case service (seed script, endpoints 1–6), Records worker tools, the agent's fake client |
| `kyb_contracts/decision.py` | `DecisionInput`, `DecisionRecord`, the status each decision leads to (`STATUS_AFTER`) and the override rule (`AGREES_WITH`, `is_override`) | review app, agent API, case service, evaluation |
| `kyb_contracts/worker_io.py` | `WorkerInput` (aliases `WorkerTask`, `FollowUp`) and `WorkerResult`, with the `CaseContext`, `Finding` and `Source` inside them | graph, workers |
| `kyb_contracts/memo.py` | `Memo` (the lead's memo), `CheckResult`, the `evidence_index` entries, `CheckedMemo` (the three together, as the review app shows them) and `Recommendation` | graph (lead, Checks), review app, case service (via `DecisionRecord`) |
| `kyb_contracts/auth.py` | The roles (`Role`, `ROLES`), `LoginRequest`, `LoginResponse`, `User`, the JWT's `TokenClaims`, and the `X-User-*` headers the gateway sets | auth service, gateway, review app |

## Rules

- **Structure only.** The contracts check types, formats and IDs, not business rules such as "an LLC must have a state ID". Missing or odd data is exactly what the agent has to handle, so the contract must be able to carry it.
- **Already parsed.** The application, vendor checks, document fields, bank verification, website fields and KYC results all arrive parsed. Workers fetch stored records by evidence ID and parse nothing.
- **No full tax IDs.** The application carries `tin_type` and `tin_last4`; owners appear as `owner:P1`. `CaseInput` and `World` reject anything that looks like a full SSN or EIN, and no file or table in this project holds one: the full numbers stay with the existing KYB system.
- **Merchant claims stay on the Evidence side.** Raw OCR and page text stay in case_db. Documents, bank verification and web snapshots are read only by the Evidence worker; owner KYC (third-party) is read by the Records worker.
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

The tests in `services/agent/tests/` check every example and every contract rule. Run them from the repo root with `pytest services/agent/tests` (they need pydantic, pytest and rapidfuzz, as in `services/agent/requirements.txt`).

## Examples

All data in `examples/` is fictional, and no full SSN or EIN appears in it: `CaseInput` and `World` reject anything that looks like one.

`world/case_1042.json` is the other file of `case_1042`, so the two together show one complete golden case (`case.json` and `world.json` in `data/golden/<ec>/<case_id>/`). How to write one: [data/golden/README.md](../data/golden/README.md).

`checked_memo.json` is `case_1042`'s memo after Checks: the memo, its `evidence_index` and the `check_result`, as the review app gets them.

The sample cases in `case_input/`:

| File | Scenario |
|---|---|
| `case_1042.json` | EC-1 + EC-2: renamed in 2025; the IRS and the DBA record still show the old name |
| `case_2001.json` | EC-4: sole proprietor with no state registration; the TIN on file is the owner's SSN |
| `case_3001.json` | EC-2: the EIN belongs to an unrelated company |