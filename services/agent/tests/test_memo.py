"""memo.py: the example validates, the structure rules hold, and the hard rules are left to Checks."""

import json
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from kyb_contracts.decision import AGREES_WITH
from kyb_contracts.decision import Recommendation as DecisionRecommendation
from kyb_contracts.memo import CheckedMemo, CheckResult, Memo, Recommendation

EXAMPLE = Path(__file__).resolve().parents[3] / "contracts" / "examples" / "checked_memo.json"


def _example() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def test_example_validates():
    checked = CheckedMemo.model_validate(_example())
    assert checked.check_result.passed
    assert checked.memo.cited_ids() == set(checked.evidence_index)


def test_recommendation_moved_here_and_decision_still_uses_it():
    assert DecisionRecommendation is Recommendation
    assert set(AGREES_WITH) == set(get_args(Recommendation))


def test_hard_rules_are_left_to_checks():
    """Official records only: CLEAR_NEEDS_TWO_SOURCES fails in Checks, but the memo is valid."""
    data = _example()["memo"]
    data["flags"][0]["evidence"] = ["R3", "R5"]
    Memo.model_validate(data)


def test_a_resolved_flag_needs_a_cause_and_evidence():
    data = _example()["memo"]
    data["flags"][0]["evidence"] = []
    with pytest.raises(ValidationError, match="resolved flag needs"):
        Memo.model_validate(data)


def test_an_unresolved_flag_may_lack_a_cause():
    data = _example()["memo"]
    data["flags"][0].update(status="unresolved", root_cause=None)
    data["recommendation"] = "insufficient_evidence"
    Memo.model_validate(data)


def test_each_flag_goes_in_one_place():
    data = _example()["memo"]
    data["out_of_scope"].append("TIN_MISMATCH")
    with pytest.raises(ValidationError, match="one place only"):
        Memo.model_validate(data)


def test_evidence_ids_must_look_like_evidence_ids():
    data = _example()["memo"]
    data["flags"][0]["evidence"].append("X9")
    with pytest.raises(ValidationError):
        Memo.model_validate(data)


def test_request_documents_needs_a_document():
    data = _example()["memo"]
    data["recommendation"] = "request_documents"
    with pytest.raises(ValidationError, match="document_request"):
        Memo.model_validate(data)
    data["document_request"] = [{"type": "irs_147c", "reason": "Shows the name the IRS has on file for the EIN"}]
    Memo.model_validate(data)


def test_an_unknown_recommendation_is_rejected():
    data = _example()["memo"]
    data["recommendation"] = "approve"   # a decision, not a recommendation
    with pytest.raises(ValidationError):
        Memo.model_validate(data)


def test_the_failed_check_from_the_walkthrough():
    result = CheckResult.model_validate({
        "passed": False,
        "failures": [{
            "rule": "CLEAR_NEEDS_TWO_SOURCES", "flag": "DBA_UNVERIFIED", "evidence": ["R3", "R5"],
            "detail": "Resolved with official records only (R3, R5). Also cite merchant evidence (D, B or W).",
        }],
    })
    assert not result.passed


@pytest.mark.parametrize("passed, failures", [
    (True, [{"rule": "NO_FULL_TAX_ID", "detail": "A full EIN appears in notes_for_analyst"}]),
    (False, []),
])
def test_passed_must_match_the_failures(passed, failures):
    with pytest.raises(ValidationError, match="passed must be true"):
        CheckResult.model_validate({"passed": passed, "failures": failures})


def test_index_types_must_fit_their_ids():
    data = _example()
    data["evidence_index"]["D1"]["type"] = "website"
    with pytest.raises(ValidationError, match="don't fit"):
        CheckedMemo.model_validate(data)


def test_the_index_holds_only_cited_ids():
    data = _example()
    data["evidence_index"]["K1"] = {"source": "KYC vendor", "type": "owner_kyc", "excerpt": "owner:P1: pass"}
    with pytest.raises(ValidationError, match="doesn't cite"):
        CheckedMemo.model_validate(data)


def test_a_missing_citation_must_fail_the_checks():
    data = _example()
    del data["evidence_index"]["W1"]
    with pytest.raises(ValidationError, match="CITED_ID_MUST_EXIST"):
        CheckedMemo.model_validate(data)
    data["check_result"] = {"passed": False, "failures": [
        {"rule": "CITED_ID_MUST_EXIST", "evidence": ["W1"], "detail": "W1 isn't in the case or the workers' sources"},
    ]}
    CheckedMemo.model_validate(data)
