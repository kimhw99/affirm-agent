"""scripts/validate_golden.py: a consistent golden case passes, and each cross-file rule holds."""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "contracts" / "examples"

_spec = importlib.util.spec_from_file_location("validate_golden", REPO / "scripts" / "validate_golden.py")
validate_golden = importlib.util.module_from_spec(_spec)
sys.modules["validate_golden"] = validate_golden
_spec.loader.exec_module(validate_golden)

from kyb_contracts.golden import RECORD_SOURCE_TYPES, WORKER_TOOLS  # noqa: E402


def _example() -> dict[str, dict]:
    """The two files of case_1042, taken from contracts/examples/."""
    sources = {"case.json": "case_input", "world.json": "world"}
    return {name: json.loads((EXAMPLES / folder / "case_1042.json").read_text(encoding="utf-8"))
            for name, folder in sources.items()}


def _with_expected() -> dict[str, dict]:
    """case_1042's two files and its expected answer, from contracts/examples/."""
    files = _example()
    files["expected.json"] = json.loads((EXAMPLES / "expected" / "case_1042.json").read_text(encoding="utf-8"))
    return files


def _write(root: Path, files: dict[str, dict], ec: str = "combo", case_id: str = "case_1042") -> Path:
    folder = root / ec / case_id
    folder.mkdir(parents=True)
    for name, data in files.items():
        (folder / name).write_text(json.dumps(data), encoding="utf-8")
    return folder


def _problems(root: Path, require_expected: bool = False) -> list[str]:
    cases, problems = validate_golden.validate(root, require_expected=require_expected)
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
    assert sum("but the folder is case_1043" in p for p in _problems(tmp_path)) == 2


def test_model_errors_are_reported_without_the_number(tmp_path, capsys):
    files = _example()
    files["case.json"]["application"]["address"] = "000-12-3456 W Fulton St"
    _write(tmp_path, files)
    assert any(p.startswith("case.json: ") and "full SSN or EIN" in p for p in _problems(tmp_path))
    assert validate_golden.main([str(tmp_path)]) == 1
    assert "000-12-3456" not in capsys.readouterr().out


def test_other_files_in_the_folder_are_ignored(tmp_path):
    folder = _write(tmp_path, _example())
    (folder / "secrets.json").write_text("not json at all", encoding="utf-8")
    assert _problems(tmp_path) == []


def test_world_needs_a_tin_record_when_the_case_has_a_tin(tmp_path):
    files = _example()
    files["world.json"]["tin_record"] = None
    _write(tmp_path, files)
    assert "world.json: tin_record is missing, but the case has a TIN" in _problems(tmp_path)


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


# ---------- expected.json ----------

def test_the_example_case_with_its_expected_answer_passes(tmp_path):
    _write(tmp_path, _with_expected())
    cases, problems = validate_golden.validate(tmp_path, require_expected=True)
    assert problems == [] and cases[0].problems == [] and cases[0].warnings == []
    assert cases[0].expected.decisions() == ["recommend_clear"]
    assert cases[0].expected.fixed_ids() == {"D1", "B1", "W1"}


def test_the_example_is_case_1042s_expected_answer():
    golden = REPO / "data" / "golden" / "combo" / "case_1042" / "expected.json"
    example = EXAMPLES / "expected" / "case_1042.json"
    assert json.loads(golden.read_text(encoding="utf-8")) == json.loads(example.read_text(encoding="utf-8"))


def test_a_missing_expected_answer_is_only_a_warning(tmp_path, capsys):
    _write(tmp_path, _example())
    cases, problems = validate_golden.validate(tmp_path)
    assert problems == [] and cases[0].problems == []
    assert cases[0].warnings == ["expected.json is missing, so the evaluation can't score this case yet"]
    assert validate_golden.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "warning: expected.json is missing" in out and "1 warning(s)" in out


def test_require_expected_makes_a_missing_answer_a_problem(tmp_path):
    _write(tmp_path, _example())
    assert "expected.json is missing" in _problems(tmp_path, require_expected=True)
    assert validate_golden.main(["--require-expected", str(tmp_path)]) == 1


def test_the_expected_answer_must_match_the_folder(tmp_path):
    files = _with_expected()
    files["expected.json"]["case_id"] = "case_1043"
    _write(tmp_path, files)
    assert "expected.json: case_id is case_1043, but the folder is case_1042" in _problems(tmp_path)


def test_every_id_the_answer_names_must_be_in_the_case(tmp_path):
    files = _with_expected()
    files["expected.json"]["must_cite"] += ["D7", {"any_of": ["W1", "W9"]}]
    files["expected.json"]["must_not_cite"] = ["K2"]
    _write(tmp_path, files)
    assert "expected.json: names D7, K2, W9, but case.json has no such evidence" in _problems(tmp_path)


@pytest.mark.parametrize("change, message", [
    ({"must_cite": ["R4"]}, "must_cite.0.id: R IDs change from run to run, so describe the record by its source"),
    ({"must_cite": ["000123456"]}, "must_cite.0.id: not a D, B, W or K evidence ID"),
    ({"must_cite": [{"source": "sos_regsitry"}]}, "must_cite.0.record.source: Input should be 'sos_registry'"),
    ({"must_cite": [{"source": "irs_tin_match", "where": {"result": "Match"}}]},
     "no Records tool writes the finding field(s) ['result']"),
    ({"must_cite": [{"any_of": ["B1"]}]}, "must_cite.0.any_of.any_of: List should have at least 2 items"),
    ({"must_cite": [{"any_of": ["B1", {"any_of": ["W1", "D1"]}]}]}, "any_of can't go inside another any_of"),
    ({"must_not_cite": [{"any_of": ["B1", "W1"]}]}, "any_of can't go inside another any_of, or in must_not_cite"),
    ({"categories": []}, "categories: List should have at least 1 item"),
    ({"categories": ["routine"]}, "categories.0: Input should be 'normal'"),
    ({"categories": ["exception", "exception"]}, "categories lists ['exception'] more than once"),
    ({"edge_cases": ["EC-3"]}, "edge_cases.0: Input should be 'EC-1'"),
    ({"expected_decision": "approve"}, "expected_decision: Input should be 'recommend_clear'"),
    ({"acceptable_decisions": ["recommend_clear"]}, "acceptable_decisions repeats expected_decision"),
    ({"must_not_cite": ["D1"]}, "['D1'] is in both must_cite and must_not_cite"),
    ({"max_follow_up_rounds": -1}, "max_follow_up_rounds: Input should be greater than or equal to 0"),
    ({"notes": ""}, "notes: String should have at least 1 character"),
    ({"notez": "a typo"}, "notez: Extra inputs are not permitted"),
])
def test_an_invalid_expected_answer_is_reported(tmp_path, change, message):
    files = _with_expected()
    files["expected.json"].update(change)
    _write(tmp_path, files)
    problems = _problems(tmp_path)
    assert any(p.startswith("expected.json: ") and message in p for p in problems), problems


def test_must_cite_forbidden_actions_and_notes_are_required(tmp_path):
    files = _with_expected()
    for name in ("must_cite", "forbidden_actions", "notes"):
        del files["expected.json"][name]
    _write(tmp_path, files)
    problems = _problems(tmp_path)
    for name in ("must_cite", "forbidden_actions", "notes"):
        assert f"expected.json: {name}: Field required" in problems


def test_a_full_tin_in_the_answer_is_reported_without_the_number(tmp_path, capsys):
    files = _with_expected()
    files["expected.json"]["notes"] = "Tested with the EIN 00-1234567."
    _write(tmp_path, files)
    assert "expected.json: a full SSN or EIN appears in the text; keep only the last 4 digits" in _problems(tmp_path)
    assert validate_golden.main([str(tmp_path)]) == 1
    assert "00-1234567" not in capsys.readouterr().out


def _with_tool_calls(tool_calls: dict) -> dict[str, dict]:
    files = _with_expected()
    files["expected.json"]["tool_calls"] = tool_calls
    return files


def test_the_example_checks_both_workers_tool_calls():
    tool_calls = _with_expected()["expected.json"]["tool_calls"]
    assert set(tool_calls) == {"records", "evidence"}
    assert {c["tool"] for c in tool_calls["evidence"]["must_call"]} == set(WORKER_TOOLS["evidence"])


def test_every_id_a_tool_call_names_must_be_in_the_case(tmp_path):
    _write(tmp_path, _with_tool_calls({"evidence": {"must_call": [{"tool": "get_document", "source": "D7"}]}}))
    assert "expected.json: names D7, but case.json has no such evidence" in _problems(tmp_path)


@pytest.mark.parametrize("tool_calls, message", [
    ({"records": {"must_call": [{"tool": "get_document", "source": "D1"}]}},
     "get_document is not one of the records worker's tools"),
    ({"evidence": {"must_call": [{"tool": "check_tin_match"}]}}, "check_tin_match is not one of the evidence worker's tools"),
    ({"records": {"must_call": [{"tool": "lookup_irs"}]}}, "lookup_irs is not a worker tool"),
    ({"records": {"must_call": [{"tool": "check_tin_match", "source": "sos_registry"}]}},
     "check_tin_match returns sources of type ['irs_tin_match']"),
    ({"records": {"must_call": [{"tool": "get_kyc_result", "source": "R2"}]}},
     "get_kyc_result returns the case's K evidence, so its source is an ID such as K1"),
    ({"evidence": {"must_call": [{"tool": "get_document", "source": "B1"}]}},
     "get_document returns the case's D evidence"),
    ({"records": {"must_call": [{"tool": "search_dba", "where": {"bank_status": "verified"}}]}},
     "no records tool writes the finding field(s) ['bank_status']"),
    ({"records": {"max_calls": 0}}, "max_calls: Input should be greater than or equal to 1"),
    ({"records": {"max_repeats": -1}}, "max_repeats: Input should be greater than or equal to 0"),
    ({"registry": {}}, "tool_calls.registry: Extra inputs are not permitted"),
])
def test_an_invalid_tool_call_expectation_is_reported(tmp_path, tool_calls, message):
    _write(tmp_path, _with_tool_calls(tool_calls))
    problems = _problems(tmp_path)
    assert any(p.startswith("expected.json: ") and message in p for p in problems), problems


def test_record_sources_are_the_ones_the_records_tools_write():
    """must_cite names R records by source type, so this list must follow the Records tools."""
    tools = REPO / "services" / "agent" / "app" / "tools"
    written: set[str] = set()
    for name in ("registry.py", "tin_kyc.py"):
        for call in re.findall(r'source_type=("\w+"(?: if [^,]+ else "\w+")?)', (tools / name).read_text(encoding="utf-8")):
            written |= set(re.findall(r'"(\w+)"', call))
    assert written - {"owner_kyc"} == RECORD_SOURCE_TYPES      # owner_kyc gives K sources, whose IDs are fixed
