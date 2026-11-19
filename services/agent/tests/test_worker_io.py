"""The contract examples validate, the cross-reference rules reject bad results, and every
sample CaseInput maps onto a CaseContext (the two contracts stay consistent)."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kyb_contracts.case_input import CaseInput
from kyb_contracts.worker_io import CaseContext, FollowUp, WorkerInput, WorkerResult, WorkerTask

EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "examples"
CASE_INPUTS = sorted((EXAMPLES / "case_input").glob("*.json"))


def _load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def context_from_case_input(case: CaseInput) -> CaseContext:
    """The lead's mapping, written out to prove every CaseInput field has a home."""
    return CaseContext.model_validate({
        "case_id": case.case_id,
        "flag_reasons": [f.value for f in case.flag_reasons],
        "application": case.application.model_dump(),
        "checks": case.checks.model_dump(),
        "documents": [{"id": d.id, "type": d.type} for d in case.documents],
        "bank_verification": {"id": case.bank_verification.id} if case.bank_verification else None,
        "web_snapshot": [{"id": w.id, "url": str(w.url), "captured_at": w.captured_at}
                         for w in case.web_snapshot],
        "owner_kyc": [{"id": k.id, "owner_ref": k.owner_ref} for k in case.owner_kyc],
    })


@pytest.mark.parametrize("path", CASE_INPUTS, ids=lambda p: p.stem)
def test_every_case_input_example_maps_to_a_context(path):
    case = CaseInput.model_validate_json(path.read_text(encoding="utf-8"))
    ctx = context_from_case_input(case)
    assert ctx.case_id == case.case_id
    assert ctx.evidence_ids() >= {d.id for d in case.documents}


def test_case_input_examples_exist():
    assert CASE_INPUTS, "contracts/examples/case_input/ is empty"


@pytest.mark.parametrize("name", ["worker_input_records.json", "worker_input_evidence.json"])
def test_input_examples_validate(name):
    task = WorkerInput.model_validate(_load(name))
    assert task.context.case_id == "case_0012"
    assert task.context.evidence_ids() == {"D1", "D2", "B1", "W1", "K1"}


def test_result_example_validates():
    result = WorkerResult.model_validate(_load("worker_result_records.json"))
    assert result.status == "completed"
    assert {f.finding_id for f in result.findings} == {"f_rec_1", "f_rec_2", "f_rec_3", "f_rec_4"}
    assert {s.source_id for s in result.sources} == {"R1", "R2", "K1"}
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
    data["sources"] = [s for s in data["sources"] if s["source_id"] != "K1"]
    with pytest.raises(ValidationError, match="unknown sources"):
        WorkerResult.model_validate(data)


def test_citation_must_match_finding_source():
    data = _load("worker_result_records.json")
    data["findings"][0]["citation"]["source_id"] = "R2"
    with pytest.raises(ValidationError, match="citation.source_id"):
        WorkerResult.model_validate(data)


def test_failed_status_needs_error():
    data = _load("worker_result_records.json")
    data["status"] = "failed"
    with pytest.raises(ValidationError, match="requires an error"):
        WorkerResult.model_validate(data)


@pytest.mark.parametrize("worker, source_id", [("records", "D1"), ("records", "W1"), ("evidence", "R1"),
                                               ("evidence", "K2")])
def test_worker_cites_only_its_side_of_the_trust_boundary(worker, source_id):
    data = _load("worker_result_records.json")
    data["worker"] = worker
    data["sources"][0]["source_id"] = source_id
    data["findings"][0]["source_id"] = source_id
    data["findings"][0]["citation"]["source_id"] = source_id
    with pytest.raises(ValidationError, match="may not cite"):
        WorkerResult.model_validate(data)


@pytest.mark.parametrize(
    "path, bad_value",
    [
        (("worker",), "lead"),
        (("max_tool_calls",), 0),
        (("record_id_start",), 0),
        (("context", "case_id"), "0012"),
        (("context", "flag_reasons"), ["EC-1"]),
        (("context", "application", "tin_last4"), "44A7"),
        (("context", "application", "tin_last4"), None),            # tin_type without last 4
        (("context", "application", "state"), "New York"),
        (("context", "application", "entity_type"), "sole_prop"),
        (("context", "application", "owners"), [{"ref": "o1", "name": "Mark Hemlock", "role": "Owner"}]),
        (("context", "documents"), [{"id": "d1", "type": "irs_147c"}]),
        (("context", "documents"), [{"id": "D9", "type": "passport"}]),
        (("context", "documents"), [{"id": "D1", "type": "irs_147c"}, {"id": "D1", "type": "w9"}]),
        (("context", "owner_kyc"), [{"id": "K1", "owner_ref": "owner:P9"}]),
        (("context", "application", "address"), "TIN 123-45-6789, 12 Main St"),
        (("context", "application", "address"), None),
        (("context", "application", "legal_name"), "Sunshine Cafe 123456789"),
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


def test_nine_digit_state_entity_id_is_allowed():
    data = _load("worker_input_records.json")
    data["context"]["application"]["state_entity_id"] = "123456789"
    WorkerInput.model_validate(data)


def test_finding_subject_must_be_business_or_owner_ref():
    data = _load("worker_result_records.json")
    data["findings"][3]["subject"] = "owner:o1"
    with pytest.raises(ValidationError):
        WorkerResult.model_validate(data)


def test_unknown_field_is_rejected():
    data = _load("worker_input_records.json")
    data["ssn"] = "123-45-6789"
    with pytest.raises(ValidationError):
        WorkerInput.model_validate(data)
