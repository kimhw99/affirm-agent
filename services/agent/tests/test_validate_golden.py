"""scripts/validate_golden.py: a consistent golden case passes, and each cross-file rule holds."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "contracts" / "examples"

_spec = importlib.util.spec_from_file_location("validate_golden", REPO / "scripts" / "validate_golden.py")
validate_golden = importlib.util.module_from_spec(_spec)
sys.modules["validate_golden"] = validate_golden
_spec.loader.exec_module(validate_golden)


def _example() -> dict[str, dict]:
    """The three files of case_1042, taken from contracts/examples/."""
    sources = {"case.json": "case_input", "world.json": "world", "secrets.json": "case_secrets"}
    return {name: json.loads((EXAMPLES / folder / "case_1042.json").read_text(encoding="utf-8"))
            for name, folder in sources.items()}


def _write(root: Path, files: dict[str, dict], ec: str = "combo", case_id: str = "case_1042") -> Path:
    folder = root / ec / case_id
    folder.mkdir(parents=True)
    for name, data in files.items():
        (folder / name).write_text(json.dumps(data), encoding="utf-8")
    return folder


def _problems(root: Path) -> list[str]:
    cases, problems = validate_golden.validate(root)
    return [problem for golden in cases for problem in golden.problems] + problems


def test_the_example_case_passes(tmp_path):
    _write(tmp_path, _example())
    assert _problems(tmp_path) == []


def test_a_case_without_a_tin_passes(tmp_path):
    files = _example()
    case = files["case.json"]
    case["application"]["tin_type"] = case["application"]["tin_last4"] = None
    del case["checks"]["tin"]
    case["flag_reasons"].remove("TIN_MISMATCH")
    files["world.json"]["tin_record"] = None
    files["secrets.json"]["tin_type"] = files["secrets.json"]["tin"] = None
    _write(tmp_path, files, ec="ec1")
    assert _problems(tmp_path) == []


def test_no_golden_cases_yet_is_fine(tmp_path, capsys):
    assert validate_golden.main([str(tmp_path)]) == 0
    assert "No golden cases" in capsys.readouterr().out


def test_a_missing_file_is_reported(tmp_path):
    files = _example()
    del files["world.json"]
    _write(tmp_path, files)
    assert "world.json is missing" in _problems(tmp_path)


def test_case_id_must_match_the_folder(tmp_path):
    _write(tmp_path, _example(), case_id="case_1043")
    assert sum("but the folder is case_1043" in p for p in _problems(tmp_path)) == 3


def test_model_errors_are_reported_without_the_number(tmp_path, capsys):
    files = _example()
    files["secrets.json"]["owners"][0]["ssn"] = "123-45-6789"
    _write(tmp_path, files)
    assert any(p.startswith("secrets.json: owners.0") and "never issued" in p for p in _problems(tmp_path))
    assert validate_golden.main([str(tmp_path)]) == 1
    assert "123-45-6789" not in capsys.readouterr().out


def test_the_full_tin_must_end_in_tin_last4(tmp_path):
    files = _example()
    files["secrets.json"]["tin"] = "00-1234560"
    _write(tmp_path, files)
    assert "secrets.json: the full TIN doesn't end in case.json's tin_last4" in _problems(tmp_path)


def test_world_needs_a_tin_record_when_the_case_has_a_tin(tmp_path):
    files = _example()
    files["world.json"]["tin_record"] = None
    _write(tmp_path, files)
    assert "world.json: tin_record is missing, but the case has a TIN" in _problems(tmp_path)


def test_secrets_owners_must_be_owners_in_the_case(tmp_path):
    files = _example()
    files["secrets.json"]["owners"][0]["ref"] = "owner:P2"
    _write(tmp_path, files)
    assert any("owners that aren't in case.json" in p for p in _problems(tmp_path))


def test_the_vendor_tin_result_must_fit_the_irs_status(tmp_path):
    files = _example()
    files["world.json"]["tin_record"].update(irs_status="not_found", irs_name=None)
    _write(tmp_path, files)
    assert any("should be issued, not not_found" in p for p in _problems(tmp_path))


def test_matched_name_must_be_a_registry_legal_name(tmp_path):
    files = _example()
    files["world.json"]["registry"][0]["legal_name"] = "NORTHLINE BICYCLE WORKS LLC"
    _write(tmp_path, files)
    assert any("checks.sos_name.matched_name" in p for p in _problems(tmp_path))


def test_a_flag_needs_its_vendor_result(tmp_path):
    files = _example()
    files["case.json"]["flag_reasons"].append("TIN_NOT_FOUND")
    _write(tmp_path, files)
    assert ("case.json: flag TIN_NOT_FOUND needs checks.tin = Not Found, but it is Mismatch"
            in _problems(tmp_path))


def test_checks_dba_needs_a_dba_in_the_application(tmp_path):
    files = _example()
    files["case.json"]["application"]["dba"] = None
    _write(tmp_path, files)
    assert "case.json: checks.dba is given, but the application has no DBA" in _problems(tmp_path)


@pytest.mark.parametrize("ec, fits", [("ec1", True), ("ec2", True), ("ec4", False), ("combo", True)])
def test_the_folder_must_fit_the_flags(tmp_path, ec, fits):
    _write(tmp_path, _example(), ec=ec)  # case_1042 has EC-1 and EC-2 flags
    assert (_problems(tmp_path) == []) is fits


def test_unknown_edge_case_folders_are_reported(tmp_path):
    _write(tmp_path, _example(), ec="ec3")
    assert any(p.startswith("ec3/ is not an edge-case folder") for p in _problems(tmp_path))


def test_record_ids_must_be_unique_across_cases(tmp_path):
    _write(tmp_path, _example())
    copy = _example()
    for data in copy.values():
        data["case_id"] = "case_1043"
    _write(tmp_path, copy, ec="ec1", case_id="case_1043")
    problems = _problems(tmp_path)
    assert any(p.startswith("registry entity IL/IL-0458821 is used by more than one case") for p in problems)
    assert any(p.startswith("DBA filing_id IL-AN-2024-0115 is used by more than one case") for p in problems)


def test_a_case_id_must_be_unique_across_folders(tmp_path):
    _write(tmp_path, _example(), ec="combo")
    _write(tmp_path, _example(), ec="ec1")
    assert any(p.startswith("case_id case_1042 is used by more than one case") for p in _problems(tmp_path))


def test_one_case_folder_can_be_checked_alone(tmp_path):
    folder = _write(tmp_path, _example())
    assert validate_golden.main([str(folder)]) == 0


def test_a_path_that_is_not_a_folder_is_a_usage_error(tmp_path):
    assert validate_golden.main([str(tmp_path / "missing")]) == 2
