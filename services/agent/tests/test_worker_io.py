"""The contract examples validate, and the cross-reference rules reject bad results."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kyb_contracts.worker_io import FollowUp, WorkerInput, WorkerResult, WorkerTask

EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "examples"


def _load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["worker_input_records.json", "worker_input_evidence.json"])
def test_input_examples_validate(name):
    task = WorkerInput.model_validate(_load(name))
    assert task.context.case_id == "case_0012"


def test_result_example_validates():
    result = WorkerResult.model_validate(_load("worker_result_records.json"))
    assert result.status == "completed"
    assert {f.finding_id for f in result.findings} == {"f_rec_1", "f_rec_2", "f_rec_3", "f_rec_4"}
    assert result.answer


def test_aliases():
    assert WorkerTask is WorkerInput
    assert FollowUp is WorkerInput


def test_result_round_trips_through_json():
    result = WorkerResult.model_validate(_load("worker_result_records.json"))
    again = WorkerResult.model_validate_json(result.model_dump_json())
    assert again == result


def test_signal_citing_unknown_finding_is_rejected():
    data = _load("worker_result_records.json")
    data["hypothesis_signals"][0]["finding_ids"].append("f_rec_99")
    with pytest.raises(ValidationError, match="unknown findings"):
        WorkerResult.model_validate(data)


def test_finding_citing_unknown_source_is_rejected():
    data = _load("worker_result_records.json")
    data["sources"] = [s for s in data["sources"] if s["source_id"] != "src_kyc_1"]
    with pytest.raises(ValidationError, match="unknown sources"):
        WorkerResult.model_validate(data)


def test_citation_must_match_finding_source():
    data = _load("worker_result_records.json")
    data["findings"][0]["citation"]["source_id"] = "src_irs_2"
    with pytest.raises(ValidationError, match="citation.source_id"):
        WorkerResult.model_validate(data)


def test_failed_status_needs_error():
    data = _load("worker_result_records.json")
    data["status"] = "failed"
    with pytest.raises(ValidationError, match="requires an error"):
        WorkerResult.model_validate(data)


@pytest.mark.parametrize(
    "path, bad_value",
    [
        (("worker",), "lead"),
        (("max_tool_calls",), 0),
        (("context", "tin_last4"), "44A7"),
        (("context", "formation_state"), "New York"),
    ],
)
def test_input_rejects_bad_values(path, bad_value):
    data = _load("worker_input_records.json")
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = bad_value
    with pytest.raises(ValidationError):
        WorkerInput.model_validate(data)


def test_unknown_field_is_rejected():
    data = _load("worker_input_records.json")
    data["ssn"] = "123-45-6789"
    with pytest.raises(ValidationError):
        WorkerInput.model_validate(data)
