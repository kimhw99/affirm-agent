"""The memo example validates, and the memo's consistency rules reject bad memos."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kyb_contracts.decision import AGREES_WITH
from kyb_contracts.memo import CheckResult, Memo, ReviewOutput, weakest

EXAMPLE = Path(__file__).resolve().parents[3] / "contracts" / "examples" / "review_output_case_1042.json"


@pytest.fixture
def data() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def _memo_error(memo: dict, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        Memo.model_validate(memo)


def test_example_validates_and_round_trips(data):
    out = ReviewOutput.model_validate(data)
    assert out.memo.recommendation == "recommend_clear"
    assert out.memo.cited_ids() == set(out.evidence_index)
    assert ReviewOutput.model_validate_json(out.model_dump_json()) == out


def test_every_recommendation_has_an_agreement_rule():
    from typing import get_args
    from kyb_contracts.memo import Recommendation
    assert set(get_args(Recommendation)) == set(AGREES_WITH)


def test_weakest_confidence():
    assert weakest(["high", "low", "medium"]) == "low"
    assert weakest(["high", "high"]) == "high"


def test_confidence_is_the_weakest_not_an_average(data):
    memo = data["memo"]
    memo["confidence"] = "high"          # one required check is medium
    _memo_error(memo, "weakest required check")


def test_hard_fail_forces_escalate(data):
    memo = data["memo"]
    memo["checks"][5].update(status="HARD_FAIL", hard_fail_reason="ein_letter_mismatch",
                             evidence_value="9999")
    _memo_error(memo, "HARD_FAIL")
    memo["recommendation"] = "escalate"
    Memo.model_validate(memo)


def test_hard_fail_needs_a_reason(data):
    memo = data["memo"]
    memo["checks"][5]["status"] = "HARD_FAIL"
    memo["recommendation"] = "escalate"
    _memo_error(memo, "hard_fail_reason")


def test_explained_without_citation_is_rejected(data):
    memo = data["memo"]
    memo["checks"][1]["explanation_evidence"] = []
    _memo_error(memo, "CONFLICT")


def test_clear_with_an_open_required_check_is_rejected(data):
    memo = data["memo"]
    memo["checks"][2]["status"] = "GAP"
    _memo_error(memo, "AGREE or EXPLAINED")


def test_open_optional_check_does_not_block_clear(data):
    memo = data["memo"]
    memo["checks"].append({
        "check_id": "ADD.second_dba", "edge_case": "EC-1", "origin": "lead_added",
        "description": "The website names a second DBA", "required": False,
        "status": "GAP", "confidence": "low", "evidence": ["W1"],
    })
    assert Memo.model_validate(memo).confidence == "medium"


def test_lead_added_checks_use_the_add_prefix(data):
    memo = data["memo"]
    memo["checks"][0]["origin"] = "lead_added"
    _memo_error(memo, "ADD. prefix")


def test_references_to_unknown_checks_are_rejected(data):
    memo = data["memo"]
    memo["flags"][0]["check_ids"].append("EC1.nope")
    _memo_error(memo, "aren't in checks")


def test_provisional_clear_needs_recheck_and_expected_absence(data):
    memo = data["memo"]
    memo["provisional"] = True
    _memo_error(memo, "recheck_after")
    memo.update(recheck_after="2026-12-17", recheck_targets=["registry"])
    _memo_error(memo, "expected_absence")
    memo["checks"][0]["expected_absence"] = True
    Memo.model_validate(memo)


def test_request_documents_needs_a_request(data):
    memo = data["memo"]
    memo["recommendation"] = "request_documents"
    _memo_error(memo, "document_request")


def test_intake_routes():
    base = {"case_id": "case_9", "generated_at": "2026-11-19T10:00:00Z", "confidence": "low"}
    with pytest.raises(ValidationError, match="route_reason"):
        Memo.model_validate({**base, "route": "human_queue", "recommendation": "insufficient_evidence"})
    Memo.model_validate({**base, "route": "human_queue", "route_reason": "no in-scope flags",
                         "recommendation": "insufficient_evidence"})
    with pytest.raises(ValidationError, match="needs_info recommends"):
        Memo.model_validate({**base, "route": "needs_info", "route_reason": "EC-2 without a TIN",
                             "recommendation": "escalate"})


def test_out_of_scope_flag_cannot_be_worked(data):
    memo = data["memo"]
    memo["flags"][0]["flag"] = "OFAC_POTENTIAL_MATCH"
    _memo_error(memo, "out of scope")


def test_only_one_merchant_follow_up(data):
    memo = data["memo"]
    merchant = {
        "check_id": "EC2.tin_match", "target": "merchant", "round": 1, "ask": "IRS 147C",
        "reason": "r", "status_before": "CONFLICT", "sent_at": "2026-11-17T15:41:00-06:00",
    }
    memo["follow_ups"] += [{**merchant, "follow_up_id": "F2"}, {**merchant, "follow_up_id": "F3"}]
    _memo_error(memo, "one merchant")


def test_internal_follow_up_needs_task_and_result(data):
    memo = data["memo"]
    del memo["follow_ups"][0]["task_id"]
    _memo_error(memo, "task_id")


def test_full_tin_is_rejected(data):
    memo = data["memo"]
    memo["notes_for_analyst"] = "EIN 00-1234567"
    _memo_error(memo, "full SSN or EIN")


def test_evidence_index_must_match_citations(data):
    extra = copy.deepcopy(data)
    extra["evidence_index"]["K1"] = {**extra["evidence_index"]["R2"], "type": "owner_kyc"}
    with pytest.raises(ValidationError, match="doesn't cite"):
        ReviewOutput.model_validate(extra)

    missing = copy.deepcopy(data)
    del missing["evidence_index"]["W1"]
    with pytest.raises(ValidationError, match="CITATION_NOT_FOUND"):
        ReviewOutput.model_validate(missing)
    missing["check_result"] = {"passed": False, "attempts": 2, "failures": [
        {"rule": "CITATION_NOT_FOUND", "detail": "W1 is not in the case"}]}
    ReviewOutput.model_validate(missing)


def test_check_result_passed_matches_failures():
    with pytest.raises(ValidationError, match="no failures"):
        CheckResult.model_validate({"passed": True, "failures": [
            {"rule": "FULL_TIN_IN_MEMO", "detail": "x"}]})
    with pytest.raises(ValidationError, match="no failures"):
        CheckResult.model_validate({"passed": False})
