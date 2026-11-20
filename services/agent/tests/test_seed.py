"""services/case/seed.py: a case and its world become rows that fit the INSERTs.

These tests need no database. Loading into a real one is described in docs/database.md.
"""

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "contracts" / "examples"

_spec = importlib.util.spec_from_file_location("seed", REPO / "services" / "case" / "seed.py")
seed = importlib.util.module_from_spec(_spec)
sys.modules["seed"] = seed
_spec.loader.exec_module(seed)

from kyb_contracts.case_input import CaseInput, Checks  # noqa: E402
from kyb_contracts.world import Officer, World  # noqa: E402


def _example() -> tuple[CaseInput, World]:
    """case_1042's case and world, from contracts/examples/."""
    case = CaseInput.model_validate_json((EXAMPLES / "case_input" / "case_1042.json").read_text(encoding="utf-8"))
    world = World.model_validate_json((EXAMPLES / "world" / "case_1042.json").read_text(encoding="utf-8"))
    return case, world


def test_every_row_fits_its_insert():
    case, world = _example()
    rows = seed.case_rows(case) | seed.world_rows(world)
    assert rows.keys() == seed.INSERTS.keys()
    for table, insert in seed.INSERTS.items():
        assert rows[table], f"case_1042 has no {table} rows"
        for row in rows[table]:
            assert len(row) == insert.count("%s"), table


def test_values_become_plain_text_and_json():
    case, world = _example()
    case_row = seed.case_rows(case)["cases"][0]
    assert type(case_row[1]) is str  # the status, not the CaseStatus enum
    assert all(type(flag) is str for flag in case_row[2])
    assert Checks.model_validate(json.loads(case_row[-1])) == case.checks

    web_row = seed.case_rows(case)["web_snapshots"][0]
    assert type(web_row[2]) is str  # the URL as text

    entity_row = seed.world_rows(world)["mock.registry_entities"][0]
    assert [Officer.model_validate(o) for o in json.loads(entity_row[8])] == world.registry[0].officers


def test_no_tin_record_and_no_bank_verification_give_no_rows():
    case, world = _example()
    case = case.model_copy(update={"bank_verification": None})
    world = world.model_copy(update={"tin_record": None})
    assert seed.case_rows(case)["bank_verifications"] == []
    assert seed.world_rows(world)["mock.tin_records"] == []
