"""World (world.json): the example validates, and the structure rules hold."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kyb_contracts.world import RegistryEntity, World

WORLD_DIR = Path(__file__).resolve().parents[3] / "contracts" / "examples" / "world"


def _world(name: str = "case_1042") -> dict:
    return json.loads((WORLD_DIR / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", sorted(WORLD_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_examples_validate(path):
    World.model_validate_json(path.read_text(encoding="utf-8"))


def test_endpoint_2_serves_the_entity_without_its_filings():
    entity = World.model_validate(_world()).registry[0]
    served = RegistryEntity.model_validate(entity.model_dump(exclude={"filings"}))
    assert served.legal_name == "NORTHLINE BICYCLE WORKS, L.L.C."
    assert [f.event_type for f in entity.filings] == ["formation", "name_change"]


def test_sole_proprietors_are_never_in_the_registry():
    data = _world()
    data["registry"][0]["entity_type"] = "SOLE_PROPRIETORSHIP"
    with pytest.raises(ValidationError, match="entity_type"):
        World.model_validate(data)


def test_filings_are_listed_oldest_first():
    data = _world()
    data["registry"][0]["filings"].reverse()
    with pytest.raises(ValidationError, match="oldest first"):
        World.model_validate(data)


def test_record_ids_are_unique_within_a_world():
    data = _world()
    data["dba_filings"].append(dict(data["dba_filings"][0]))
    with pytest.raises(ValidationError, match="duplicate DBA filing_id"):
        World.model_validate(data)


def test_an_individual_registrant_has_no_state_entity_id():
    data = _world()
    data["dba_filings"][0]["registrant_type"] = "individual"
    with pytest.raises(ValidationError, match="individual"):
        World.model_validate(data)


@pytest.mark.parametrize("status", ["not_issued", "not_found"])
def test_irs_name_only_when_issued(status):
    data = _world()
    data["tin_record"]["irs_status"] = status
    with pytest.raises(ValidationError, match="irs_name"):
        World.model_validate(data)


def test_a_commercial_profile_needs_its_state():
    data = _world()
    del data["commercial"][0]["state"]
    with pytest.raises(ValidationError, match="state"):
        World.model_validate(data)


def test_a_case_without_a_tin_has_no_tin_record():
    data = _world()
    data["tin_record"] = None
    assert World.model_validate(data).tin_record is None


@pytest.mark.parametrize("value", ["00-1234567", "000-12-3456", "001234567"])
def test_full_tax_id_is_rejected_anywhere(value):
    data = _world()
    data["registry"][0]["registered_address"] += f" {value}"
    with pytest.raises(ValidationError, match="full SSN or EIN"):
        World.model_validate(data)


def test_nine_digit_entity_ids_are_not_mistaken_for_tax_ids():
    data = _world()
    entity = data["registry"][0]
    entity["state_entity_id"] = "123456789"
    for i, filing in enumerate(entity["filings"], start=1):
        filing["event_id"] = f"123456789-{i}"
    data["dba_filings"][0]["registrant_state_entity_id"] = "123456789"
    World.model_validate(data)
