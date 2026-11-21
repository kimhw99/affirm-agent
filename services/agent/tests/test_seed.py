"""services/case/seed.py: a case and its world become rows that fit the INSERTs.

These tests need no database. Loading into a real one is described in docs/database.md.
"""

import importlib.util
import json
import shutil
import sys
import types
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


class FakeConnection:
    """Stands in for psycopg's connection, and records every statement and its values."""

    def __init__(self):
        self.statements: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        return []          # no case has decisions or audit log entries yet

    def rollback(self):
        pass

    def cursor(self):
        return self

    def executemany(self, sql, rows):
        self.statements.append((sql, list(rows)))


def test_the_expected_answer_never_reaches_the_database(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "combo" / "case_1042"
    shutil.copytree(REPO / "data" / "golden" / "combo" / "case_1042", folder)
    expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
    expected["notes"] = "ANSWER-ONLY: if this reaches case_db, the agent can read the answer."
    (folder / "expected.json").write_text(json.dumps(expected), encoding="utf-8")

    conn = FakeConnection()
    fake_psycopg = types.SimpleNamespace(connect=lambda **_: conn, Error=Exception, OperationalError=Exception)
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setenv("CASE_DB_PASSWORD", "only-for-this-test")

    assert seed.main([str(tmp_path)]) == 0, capsys.readouterr().out
    cases, _ = seed.validate_golden.validate(tmp_path)
    assert cases[0].expected is not None           # the answer was there, and valid
    inserted = {sql.split("INTO", 1)[1].split()[0] for sql, _ in conn.statements if "INSERT INTO" in sql}
    assert inserted == set(seed.INSERTS)            # case.json's and world.json's tables, nothing else
    values = json.dumps([params for _, params in conn.statements], default=str)
    assert "ANSWER-ONLY" not in values
    assert "expected_decision" not in values and "must_cite" not in values
