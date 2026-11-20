"""review.py: the example validates, and the Lead report's consistency rules reject bad reports.

memo.py (the agreed memo, CheckedMemo) has its own tests in test_memo.py.
"""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kyb_contracts.memo import CheckedMemo
from kyb_contracts.review import FLAG_EDGE_CASE, LeadReport, ReviewOutput, weakest
from kyb_contracts.case_input import FlagReason

EXAMPLE = Path(__file__).resolve().parents[3] / "contracts" / "examples" / "review_output_case_1042.json"


@pytest.fixture
def data() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def _check(data: dict, check_id: str) -> dict:
    return next(c for c in data["lead"]["checks"] if c["check_id"] == check_id)


def _error(data: dict, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        ReviewOutput.model_validate(data)


def test_example_validates_and_round_trips(data):
    out = ReviewOutput.model_validate(data)
    assert out.recommendation == "recommend_clear" and out.passed
    # The memo part is memo.py's CheckedMemo, untouched.
    CheckedMemo.model_validate(data["checked_memo"])
    assert out.memo.cited_ids() == set(out.checked_memo.evidence_index)
    assert ReviewOutput.model_validate_json(out.model_dump_json()) == out


def test_in_scope_flags_have_an_edge_case():
    out_of_scope = {FlagReason.PRODUCT_CATEGORY_REVIEW, FlagReason.OFAC_POTENTIAL_MATCH}
    assert set(FLAG_EDGE_CASE) == set(FlagReason) - out_of_scope


def test_weakest_confidence():
    assert weakest(["high", "low", "medium"]) == "low"
    assert weakest(["high", "high"]) == "high"


def test_confidence_is_the_weakest_not_an_average(data):
    data["lead"]["confidence"] = "high"          # one required check is medium
    _error(data, "weakest required check")


def test_hard_fail_forces_escalate(data):
    _check(data, "EC2.ein_letter_matches").update(status="HARD_FAIL", hard_fail_reason="ein_letter_mismatch",
                                                  evidence_value="9999", confidence="high")
    data["checked_memo"]["memo"]["recommendation"] = "insufficient_evidence"
    _error(data, "HARD_FAIL")
    data["checked_memo"]["memo"]["recommendation"] = "escalate"
    assert ReviewOutput.model_validate(data).recommendation == "escalate"


def test_hard_fail_needs_a_reason(data):
    _check(data, "EC2.ein_letter_matches")["status"] = "HARD_FAIL"
    _error(data, "hard_fail_reason")


def test_explained_without_citation_is_rejected(data):
    _check(data, "EC2.tin_match")["explanation_evidence"] = []
    _error(data, "CONFLICT")


def test_clear_with_an_open_required_check_is_rejected(data):
    _check(data, "EC1.dba_owner")["status"] = "GAP"
    _error(data, "AGREE or EXPLAINED")


def test_open_optional_check_does_not_block_clear(data):
    data["lead"]["checks"].append({
        "check_id": "ADD.second_dba", "edge_case": "EC-1", "origin": "lead_added",
        "description": "The website names a second DBA", "required": False,
        "status": "GAP", "confidence": "low", "evidence": ["W1"],
    })
    data["report_evidence_index"]["W1"] = {"source": "https://x.example", "type": "website", "excerpt": "dba: X"}
    assert ReviewOutput.model_validate(data).lead.confidence == "medium"


def test_lead_added_checks_use_the_add_prefix(data):
    data["lead"]["checks"][0]["origin"] = "lead_added"
    _error(data, "ADD. prefix")


def test_references_to_unknown_checks_are_rejected(data):
    data["lead"]["flag_checks"][0]["check_ids"].append("EC1.nope")
    _error(data, "aren't in checks")


def test_flag_checks_must_name_memo_flags(data):
    data["lead"]["flag_checks"].append({"flag": "TIN_NOT_FOUND", "check_ids": []})
    _error(data, "flag_checks names a flag")


def test_provisional_clear_needs_recheck_and_expected_absence(data):
    data["lead"]["provisional"] = True
    _error(data, "recheck_after")
    data["lead"].update(recheck_after="2026-12-19", recheck_targets=["registry"])
    _error(data, "expected_absence")
    data["lead"]["checks"][0]["expected_absence"] = True
    assert ReviewOutput.model_validate(data).lead.provisional


def test_human_queue_without_flags_has_no_memo():
    lead = {"case_id": "case_9", "generated_at": "2026-11-19T10:00:00Z", "confidence": "low",
            "route": "human_queue"}
    with pytest.raises(ValidationError, match="route_reason"):
        LeadReport.model_validate(lead)
    lead["route_reason"] = "no flags on the case"
    out = ReviewOutput.model_validate({"lead": lead})
    assert out.memo is None and out.recommendation == "insufficient_evidence"
    with pytest.raises(ValidationError, match="only a human_queue"):
        ReviewOutput.model_validate({"lead": {**lead, "route": "reviewed"}})


def test_needs_info_recommends_request_documents(data):
    data["lead"].update(route="needs_info", route_reason="EC-2 without a TIN")
    _error(data, "needs_info recommends")


def test_only_one_merchant_follow_up(data):
    merchant = {
        "check_id": "EC2.tin_match", "target": "merchant", "round": 1, "ask": "IRS 147C",
        "reason": "r", "status_before": "CONFLICT", "sent_at": "2026-11-17T15:41:00-06:00",
    }
    data["lead"]["follow_ups"] += [{**merchant, "follow_up_id": "F8"}, {**merchant, "follow_up_id": "F9"}]
    _error(data, "one merchant")


def test_internal_follow_up_needs_task_and_result(data):
    data["lead"]["follow_ups"] = [{
        "follow_up_id": "F1", "check_id": "EC2.tin_match", "target": "records", "round": 1,
        "ask": "Run the TIN match again", "reason": "r", "status_before": "CONFLICT",
        "sent_at": "2026-11-17T15:41:00-06:00",
    }]
    _error(data, "task_id")


def test_report_index_holds_only_ids_the_report_cites(data):
    data["report_evidence_index"]["K1"] = {"source": "KYC", "type": "owner_kyc", "excerpt": "pass"}
    _error(data, "doesn't cite")


def test_the_memo_part_keeps_memo_py_rules(data):
    broken = copy.deepcopy(data)
    del broken["checked_memo"]["evidence_index"]["D1"]
    _error(broken, "CITED_ID_MUST_EXIST")
