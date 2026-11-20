"""Plans start from fixed templates; the LLM can add optional checks but never remove required ones."""

from datetime import timedelta

import pytest

from conftest import TODAY
from kyb_contracts.case_input import CaseInput, FlagReason

from app.graph.orchestrator import route_case
from app.graph.plan import AddedCheck, PlanExtras, build_plan, initial_tasks, template_checks


def _plan(raw, extras=None):
    d, case = route_case(raw)
    return build_plan(case, d, TODAY, extras), case, d


def test_case_1042_template(case_1042):
    plan, _, _ = _plan(case_1042)
    assert [c.check_id for c in plan.checks] == [
        "EC1.entity_active", "EC1.dba_owner", "EC1.bank_account_holder",
        "EC2.tin_match", "EC2.name_control", "EC2.ein_letter_matches", "EC2.tin_type_fits",
    ]
    assert all(c.required and c.origin == "template" for c in plan.checks)
    assert plan.checks_for_flag(FlagReason.DBA_UNVERIFIED) == [
        "EC1.entity_active", "EC1.dba_owner", "EC1.bank_account_holder"]


def test_llm_can_add_but_not_remove(case_1042):
    extras = PlanExtras(added_checks=[
        AddedCheck(slug="Second DBA!", edge_case="EC-1", description="The website names a second DBA",
                   evidence_focus=["dba"]),
        AddedCheck(slug="tin_match", edge_case="EC-2", description="dup"),
    ])
    plan, _, _ = _plan(case_1042, extras)
    added = [c for c in plan.checks if c.origin == "lead_added"]
    assert [c.check_id for c in added] == ["ADD.second_dba", "ADD.tin_match"]
    assert not any(c.required for c in added)                        # never a new blocker
    base, _, _ = _plan(case_1042)
    assert set(base.required_ids()) == set(plan.required_ids())     # nothing removed


def test_sole_proprietor_expects_registry_absence(case_2001):
    plan, _, _ = _plan(case_2001)
    ids = [c.check_id for c in plan.checks]
    assert "EC1.entity_active" not in ids and "EC1.dba_owner" not in ids
    assert {"EC4.registry_presence", "EC4.sole_prop_owner", "EC4.sole_prop_dba"} <= set(ids)
    assert "EC2.ein_letter_matches" not in ids                       # an SSN has no EIN letter
    assert plan.check("EC4.registry_presence").expected_absence is True


@pytest.mark.parametrize("age_days, expected", [(10, True), (400, False)])
def test_new_llc_expects_registry_absence_only_when_recent(case_3001, age_days, expected):
    case_3001["flag_reasons"] = ["ENTITY_NOT_FOUND"]
    case_3001["application"]["formation_date"] = (TODAY - timedelta(days=age_days)).isoformat()
    checks = template_checks(CaseInput.model_validate(case_3001), [FlagReason.ENTITY_NOT_FOUND], TODAY)
    reg = next(c for c in checks if c.check_id == "EC4.registry_presence")
    assert reg.expected_absence is expected


def test_worker_tasks_carry_keys_not_document_text(case_1042):
    plan, case, d = _plan(case_1042)
    tasks = initial_tasks(case, d, plan)
    assert [t.worker for t in tasks] == ["records", "evidence"]
    dumped = tasks[1].model_dump_json()
    assert "HARBOR LIGHT" not in dumped                              # D1's extracted name stays out
    assert tasks[1].context.documents[0].id == "D1"
    assert all(1 <= t.max_tool_calls <= 20 for t in tasks)
