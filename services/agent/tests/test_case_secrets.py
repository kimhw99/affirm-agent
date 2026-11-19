"""CaseSecrets (secrets.json): the example validates, and only fake numbers get in."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kyb_contracts.case_secrets import CaseSecrets

SECRETS_DIR = Path(__file__).resolve().parents[3] / "contracts" / "examples" / "case_secrets"


def _secrets(name: str = "case_1042") -> dict:
    return json.loads((SECRETS_DIR / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", sorted(SECRETS_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_examples_validate(path):
    CaseSecrets.model_validate_json(path.read_text(encoding="utf-8"))


def test_a_sole_proprietor_files_under_the_owners_ssn():
    CaseSecrets.model_validate({
        "case_id": "case_2001", "tin_type": "SSN", "tin": "000-12-3456",
        "owners": [{"ref": "owner:P1", "ssn": "000-12-3456"}],
    })


def test_a_case_may_have_no_tin():
    secrets = CaseSecrets.model_validate({"case_id": "case_1042", "tin_type": None, "tin": None})
    assert secrets.owners == []


def test_tin_type_and_tin_go_together():
    data = _secrets()
    data["tin"] = None
    with pytest.raises(ValidationError, match="together"):
        CaseSecrets.model_validate(data)


@pytest.mark.parametrize("tin_type, tin", [
    ("EIN", "12-3456789"),    # a prefix the IRS issues
    ("EIN", "001234567"),     # no dash
    ("EIN", "000-12-3456"),   # an SSN given as an EIN
    ("SSN", "123-45-6789"),   # an area number that is issued
])
def test_only_fake_tins_get_in(tin_type, tin):
    data = _secrets()
    data["tin_type"], data["tin"] = tin_type, tin
    with pytest.raises(ValidationError, match="never issued"):
        CaseSecrets.model_validate(data)


def test_only_fake_ssns_get_in():
    data = _secrets()
    data["owners"][0]["ssn"] = "123-45-6789"
    with pytest.raises(ValidationError, match="never issued"):
        CaseSecrets.model_validate(data)


def test_numbers_stay_out_of_errors_and_repr():
    data = _secrets()
    data["owners"][0]["ssn"] = "123-45-6789"
    with pytest.raises(ValidationError) as excinfo:
        CaseSecrets.model_validate(data)
    assert "123-45-6789" not in str(excinfo.value)

    text = repr(CaseSecrets.model_validate(_secrets()))
    assert "00-1234567" not in text and "000-31-7742" not in text


def test_owner_refs_are_unique():
    data = _secrets()
    data["owners"].append(dict(data["owners"][0]))
    with pytest.raises(ValidationError, match="duplicate owner refs"):
        CaseSecrets.model_validate(data)


def test_unknown_fields_are_still_errors():
    data = _secrets()
    data["ein"] = "00-1234567"
    with pytest.raises(ValidationError, match="ein"):
        CaseSecrets.model_validate(data)
