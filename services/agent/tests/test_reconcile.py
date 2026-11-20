"""Reconcile compares in code first; the LLM only judges EXPLAINED, with citations."""

from conftest import TODAY
from fakes import Rec, ScriptedWorker, evidence_1042, kyc, records_1042

from app.graph.facts import FactIndex
from app.graph.orchestrator import route_case
from app.graph.plan import build_plan, initial_tasks
from app.graph.reconcile import Explanation, apply_explanation, assess, decide, reconcile


def _run(raw, records_script, evidence_script, explainer=None):
    d, case = route_case(raw)
    plan = build_plan(case, d, TODAY)
    results = []
    for t in initial_tasks(case, d, plan):
        script = records_script if t.worker == "records" else evidence_script
        results.append((0, ScriptedWorker(t.worker, script).run(t)))
    facts = FactIndex(case, results)
    outcomes, assessments = reconcile(case, plan, facts, TODAY, explainer)
    return outcomes, assessments, plan, facts, case


def test_case_1042_first_round(case_1042):
    outcomes, *_ = _run(case_1042, records_1042, evidence_1042)
    status = {k: o.status for k, o in outcomes.items()}
    assert status == {
        "EC1.entity_active": "AGREE",          # L.L.C. vs LLC is formatting only
        "EC1.dba_owner": "CONFLICT",           # filed by HARBOR LIGHT CYCLES LLC
        "EC1.bank_account_holder": "AGREE",
        "EC2.tin_match": "CONFLICT",           # mismatch; the letter names another company
        "EC2.name_control": "GAP",             # nothing the IRS accepted yet
        "EC2.ein_letter_matches": "AGREE",
        "EC2.tin_type_fits": "AGREE",
    }
    assert decide(outcomes) == "follow_up"


def test_ein_letter_mismatch_is_a_hard_fail_the_llm_cannot_override(case_1042):
    def evidence(task):
        recs = evidence_1042(task)
        recs[0].findings[1] = ("ein_last4", "9999", "EIN ending 9999")
        return recs

    always = lambda check, a, facts: Explanation("trust me", ["R1"])
    outcomes, *_ = _run(case_1042, records_1042, evidence, explainer=always)
    o = outcomes["EC2.ein_letter_matches"]
    assert (o.status, o.hard_fail_reason) == ("HARD_FAIL", "ein_letter_mismatch")
    assert decide(outcomes) == "hard_fail"


def test_explanation_without_citation_stays_a_conflict(case_1042):
    _, assessments, plan, facts, _ = _run(case_1042, records_1042, evidence_1042)
    a = assessments["EC1.dba_owner"]
    out = apply_explanation(plan.check("EC1.dba_owner"), a, facts, Explanation("It's a former name", []))
    assert out.status == "CONFLICT"
    out = apply_explanation(plan.check("EC1.dba_owner"), a, facts, Explanation("It's a former name", ["X9"]))
    assert out.status == "CONFLICT"


def test_website_alone_cannot_explain_a_difference(case_1042):
    _, assessments, plan, facts, _ = _run(case_1042, records_1042, evidence_1042)
    a = assessments["EC1.dba_owner"]
    out = apply_explanation(plan.check("EC1.dba_owner"), a, facts, Explanation("The site says so", ["W1"]))
    assert out.status == "CONFLICT"


def test_explanation_must_cite_real_findings(case_1042):
    _, assessments, plan, facts, _ = _run(case_1042, records_1042, evidence_1042)
    a = assessments["EC1.dba_owner"]
    key = next(f.key for f in facts.facts if f.field == "dba_owner")
    out = apply_explanation(plan.check("EC1.dba_owner"), a, facts, Explanation("Former name", [key]))
    assert out.status == "EXPLAINED" and out.explanation_evidence == [facts.by_key(key).source_id]


def test_website_cannot_overrule_the_registry(case_1042):
    def evidence(task):
        recs = evidence_1042(task)
        recs[2].findings[0] = ("legal_name", "Totally Different Name LLC", "legal name")
        return recs
    outcomes, *_ = _run(case_1042, records_1042, evidence)
    assert outcomes["EC1.entity_active"].status == "AGREE"         # bank + registry agree; W1 ignored


def test_website_only_support_is_low_confidence(case_1042):
    def evidence(task):
        return [r for r in evidence_1042(task) if r.source_id == "W1"]
    outcomes, *_ = _run(case_1042, records_1042, evidence)
    o = outcomes["EC1.entity_active"]
    assert o.status == "AGREE" and o.confidence == "low"


def test_failed_kyc_is_a_hard_fail(case_1042):
    def records(task):
        return [r if r.source_id != "K1" else kyc(result="fail") for r in records_1042(task)]
    outcomes, *_ = _run(case_1042, records, evidence_1042)
    assert any(o.hard_fail_reason == "kyc_failed" for o in outcomes.values())
    assert decide(outcomes) == "hard_fail"


def test_dissolved_entity_is_a_hard_fail(case_1042):
    def records(task):
        recs = records_1042(task)
        recs[0] = Rec("sos_registry", [("legal_name", "NORTHLINE BICYCLE WORKS LLC", "x"),
                                       ("entity_status", "dissolved", "Dissolved")])
        return recs
    outcomes, *_ = _run(case_1042, records, evidence_1042)
    o = outcomes["EC1.entity_active"]
    assert (o.status, o.hard_fail_reason) == ("HARD_FAIL", "entity_dissolved_or_revoked")


def test_tampered_document_is_a_hard_fail(case_1042):
    def evidence(task):
        recs = evidence_1042(task)
        recs[0].findings.append(("document_tampered", True, "font mismatch on the EIN"))
        return recs
    outcomes, *_ = _run(case_1042, records_1042, evidence)
    assert any(o.hard_fail_reason == "document_tampered" for o in outcomes.values())


def _tin_not_issued(letter_date):
    def records(task):
        return [Rec("sos_registry", [("legal_name", "MAPLE RIDGE COFFEE ROASTERS LLC", "x"),
                                     ("entity_status", "active", "Active")]),
                Rec("irs_tin_match", [("tin_match", "2", "code 2: TIN not currently issued ('MAPLE RIDGE COFFEE ROASTERS LLC')")])]

    def evidence(task):
        return [Rec("irs_cp575", [("name", "MAPLE RIDGE COFFEE ROASTERS LLC", "n"), ("ein_last4", "4321", "4321"),
                                  ("date", letter_date, letter_date)], source_id="D1", authority="merchant")]
    return records, evidence


def test_fresh_ein_not_yet_issued_is_a_gap(case_3001):
    case_3001["documents"][0].update(type="irs_cp575")
    outcomes, *_ = _run(case_3001, *_tin_not_issued("2026-11-01"))
    assert outcomes["EC2.tin_match"].status == "GAP"


def test_old_ein_never_issued_is_a_hard_fail(case_3001):
    case_3001["documents"][0].update(type="irs_cp575")
    outcomes, *_ = _run(case_3001, *_tin_not_issued("2024-01-01"))
    o = outcomes["EC2.tin_match"]
    assert (o.status, o.hard_fail_reason) == ("HARD_FAIL", "tin_never_issued")


def test_only_named_checks_are_re_reconciled(case_1042):
    outcomes, _, plan, facts, case = _run(case_1042, records_1042, evidence_1042)
    sentinel = outcomes["EC1.entity_active"].model_copy(update={"confidence": "low"})
    prev = {**outcomes, "EC1.entity_active": sentinel}
    again, assessed = reconcile(case, plan, facts, TODAY, only=["EC2.tin_match"], previous=prev)
    assert set(assessed) == {"EC2.tin_match"}
    assert again["EC1.entity_active"].confidence == "low"          # untouched
