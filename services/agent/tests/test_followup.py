"""Follow-ups are targeted (one check, one side, one thing); the stopping rules are code."""

from datetime import datetime, timezone

import pytest

from kyb_contracts.case_input import CaseInput
from app.review import CheckOutcome

from app.graph.facts import FactIndex
from app.graph.followup import (
    MAX_INTERNAL_ROUNDS,
    PLAYBOOK,
    merchant_requests,
    next_follow_ups,
    safe_ask,
    stop_reason,
)

NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)


def _o(cid, status, edge="EC-2", required=True, **kw):
    if status == "HARD_FAIL":
        kw.setdefault("hard_fail_reason", "ein_letter_mismatch")
    return CheckOutcome(check_id=cid, edge_case=edge, description=cid, status=status, confidence="low",
                        required=required, **kw)


@pytest.mark.parametrize("outcomes, rounds, new, cands, expected", [
    ({"EC2.tin_match": _o("EC2.tin_match", "HARD_FAIL")}, 0, None, True, "hard_fail"),
    ({"EC2.tin_match": _o("EC2.tin_match", "AGREE")}, 0, None, True, "resolved"),
    ({"EC2.tin_match": _o("EC2.tin_match", "CONFLICT")}, MAX_INTERNAL_ROUNDS, True, True, "max_rounds"),
    ({"EC2.tin_match": _o("EC2.tin_match", "CONFLICT")}, 1, False, True, "no_new_findings"),
    ({"EC2.tin_match": _o("EC2.tin_match", "GAP")}, 1, True, False, "no_internal_follow_up_left"),
    ({"EC2.tin_match": _o("EC2.tin_match", "CONFLICT")}, 1, True, True, None),
    # An open *optional* check doesn't keep the loop going.
    ({"EC2.tin_match": _o("EC2.tin_match", "AGREE"), "ADD.x": _o("ADD.x", "GAP", required=False,
                                                                  origin="lead_added")}, 0, None, True, "resolved"),
])
def test_stop_rules(outcomes, rounds, new, cands, expected):
    assert stop_reason(outcomes, rounds, new, cands) == expected


def test_one_targeted_follow_up_per_open_check(case_1042):
    case = CaseInput.model_validate(case_1042)
    facts = FactIndex(case, [])
    outcomes = {
        "EC1.dba_owner": _o("EC1.dba_owner", "CONFLICT", edge="EC-1"),
        "EC2.tin_match": _o("EC2.tin_match", "CONFLICT"),
        "EC2.ein_letter_matches": _o("EC2.ein_letter_matches", "AGREE"),
    }
    fus = next_follow_ups(case, outcomes, facts, [], 1, NOW)
    assert [(f.check_id, f.target) for f in fus] == [("EC1.dba_owner", "records"), ("EC2.tin_match", "records")]
    assert "county-level DBA" in fus[0].ask and "Northline Cycles" in fus[0].ask
    assert [f.follow_up_id for f in fus] == ["F1", "F2"]

    # Round 2 uses the next step in the playbook; when the steps run out, nothing is sent.
    history = [{"check_id": f.check_id, "target": f.target} for f in fus]
    fus2 = next_follow_ups(case, outcomes, facts, history, 2, NOW)
    assert all("filing history" in f.ask for f in fus2)
    history += [{"check_id": f.check_id, "target": f.target} for f in fus2]
    assert next_follow_ups(case, outcomes, facts, history, 3, NOW) == []


def test_merchant_request_never_asks_for_a_document_already_in_the_case(case_3001):
    case = CaseInput.model_validate(case_3001)       # already has an irs_147c
    reqs = merchant_requests(case, [_o("EC2.ein_letter_matches", "GAP"), _o("EC2.tin_match", "CONFLICT")])
    assert [r.document_type for r in reqs] == ["irs_cp575"]


def test_every_template_check_has_a_playbook_entry():
    from app.graph.plan import TEMPLATES
    assert {t.check_id for t in TEMPLATES} <= set(PLAYBOOK)


def test_llm_rewording_is_only_accepted_when_safe():
    assert safe_ask(None, "fallback") == "fallback"
    assert safe_ask("Send EIN 12-3456789", "fallback") == "fallback"
    assert safe_ask("x" * 500, "fallback") == "fallback"
    assert safe_ask("Ask the IRS for the 147C.", "fallback") == "Ask the IRS for the 147C."
