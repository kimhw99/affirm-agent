"""Reconcile compares in code first; the LLM only judges EXPLAINED, with citations."""

import json
from pathlib import Path

from conftest import TODAY
from fakes import (ScriptedWorker, bank, document, evidence_1042, kyc, records_1042, registry_entity, tin_match,
                   website)

from app.graph.facts import FactIndex
from app.graph.orchestrator import route_case
from app.graph.plan import build_plan, initial_tasks
from app.graph.reconcile import Explanation, apply_explanation, assess, decide, reconcile

FIXTURES = Path(__file__).resolve().parent / "fixtures"


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
    key = next(f.key for f in facts.records("dba_owner"))
    out = apply_explanation(plan.check("EC1.dba_owner"), a, facts, Explanation("Former name", [key]))
    assert out.status == "EXPLAINED" and out.explanation_evidence == [facts.by_key(key).source_id]


def test_website_cannot_overrule_the_registry(case_1042):
    def evidence(task):
        recs = evidence_1042(task)
        recs[2].findings[0] = ("website_legal_name", "Totally Different Name LLC", "extracted.legal_name: x")
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
        recs[0] = registry_entity("IL-0458821", "NORTHLINE BICYCLE WORKS LLC", status="dissolved")
        return recs
    outcomes, *_ = _run(case_1042, records, evidence_1042)
    o = outcomes["EC1.entity_active"]
    assert (o.status, o.hard_fail_reason) == ("HARD_FAIL", "entity_dissolved_or_revoked")


def test_low_extraction_confidence_is_not_treated_as_tampering(case_1042):
    """No worker can report tampering; a shaky extraction (what the tools do report) is not a hard fail."""
    def evidence(task):
        recs = evidence_1042(task)
        recs[0].findings.append(("low_extraction_confidence", True, "confidence: 0.55"))
        return recs
    outcomes, *_ = _run(case_1042, records_1042, evidence)
    assert not any(o.status == "HARD_FAIL" for o in outcomes.values())


def _case_4401(**app_changes):
    raw = json.loads((FIXTURES / "ec4" / "case_4401" / "case.json").read_text(encoding="utf-8"))
    raw["application"].update(app_changes)
    return raw


def _records_with_status(status, formed="2026-11-09"):
    return lambda task: [registry_entity("CO-20261488", "JUNIPER & PINE CANDLE CO. LLC", status=status,
                                         formed=formed, state="CO")]


def test_pending_registry_filing_of_a_new_business_is_explained():
    """world.EntityStatus 'pending' is the pending filing: no separate (unfeedable) pending_filing fact."""
    outcomes, *_ = _run(_case_4401(), _records_with_status("pending"), lambda task: [])
    o = outcomes["EC4.registry_presence"]
    assert o.expected_absence and o.status == "EXPLAINED" and "pending" in o.explanation
    assert o.explanation_evidence == ["R1"]


def test_pending_registry_filing_of_an_old_business_stays_open():
    raw = _case_4401(formation_date="2019-01-15")
    raw["documents"] = [d for d in raw["documents"] if d["type"] != "articles"]
    outcomes, *_ = _run(raw, _records_with_status("pending", formed="2019-01-15"), lambda task: [])
    o = outcomes["EC4.registry_presence"]
    assert not o.expected_absence and o.status == "GAP"


def test_operating_needs_no_domain_age():
    """EC4.operating closes on what the tools report: a verified bank plus a license or website."""
    def evidence(task):
        return [bank("JUNIPER & PINE CANDLE CO LLC"), website(legal_name="Juniper & Pine Candle Co. LLC")]
    raw = _case_4401()
    raw["flag_reasons"] = ["NO_COMMERCIAL_RECORD"]
    raw["bank_verification"] = {"id": "B1", "account_holder": "JUNIPER & PINE CANDLE CO LLC", "status": "verified",
                                "verified_at": "2026-11-15"}
    raw["web_snapshot"] = [{"id": "W1", "url": "https://juniper.example", "captured_at": "2026-11-15",
                            "extracted": {"legal_name": "Juniper & Pine Candle Co. LLC"}}]
    outcomes, *_ = _run(raw, _records_with_status("active"), evidence)
    assert outcomes["EC4.operating"].status == "AGREE"


def _tin_not_issued(letter_date):
    def records(task):
        return [registry_entity("OH-5582114", "MAPLE RIDGE COFFEE ROASTERS LLC", state="OH"),
                tin_match("case_3001", "Maple Ridge Coffee Roasters LLC", "Not Issued")]

    def evidence(task):
        return [document("D1", "irs_cp575", name="MAPLE RIDGE COFFEE ROASTERS LLC", ein_last4="4321",
                         date=letter_date)]
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


# ---------- How the Lead reads the BEAC-7 workers' findings ----------

def test_look_alike_entity_from_a_search_is_not_the_applicant(case_1042):
    """search_registry returns look-alikes; a dissolved one must not fail the applicant."""
    def records(task):
        return [registry_entity("IL-0458821", "NORTHLINE BICYCLE WORKS, L.L.C."),
                registry_entity("IL-0999001", "NORTHLINE BICYCLES INC", status="dissolved")] + records_1042(task)[1:]
    outcomes, _, _, facts, _ = _run(case_1042, records, evidence_1042)
    assert outcomes["EC1.entity_active"].status == "AGREE"
    assert not any(o.status == "HARD_FAIL" for o in outcomes.values())
    assert [f.value for f in facts.records("separate_entity")] == ["NORTHLINE BICYCLES INC"]


def test_tin_matching_a_separate_registry_entity_is_a_hard_fail(case_3001):
    def records(task):
        return [registry_entity("OH-5582114", "MAPLE RIDGE COFFEE ROASTERS LLC", state="OH"),
                registry_entity("OH-1100", "BLUE RIVER TOOLS INC", state="OH"),
                tin_match("case_3001", "Maple Ridge Coffee Roasters LLC", "Mismatch"),
                tin_match("case_3001", "BLUE RIVER TOOLS INC", "Match")]
    from fakes import evidence_3001
    outcomes, *_ = _run(case_3001, records, evidence_3001)
    o = outcomes["EC2.tin_match"]
    assert (o.status, o.hard_fail_reason) == ("HARD_FAIL", "ein_belongs_to_other_entity")


def test_name_tested_comes_from_the_tin_source(case_1042):
    from app.graph.reconcile import tin_results
    *_, facts, case = _run(case_1042, records_1042, evidence_1042)
    assert [(r.result, r.name_tested) for r in tin_results(facts, case)] == [("Mismatch", "Northline Bicycle Works LLC")]


def test_dba_filed_under_the_applicants_entity_number_belongs_to_it(case_1042):
    from fakes import dba_filing

    def records(task):
        recs = records_1042(task)
        recs[1] = dba_filing("IL-AN-2024-0115", "NORTHLINE CYCLES", "HARBOR LIGHT CYCLES LLC", "IL-0458821")
        return recs
    outcomes, *_ = _run(case_1042, records, evidence_1042)
    assert outcomes["EC1.dba_owner"].status == "AGREE"


def test_applied_dba_as_legal_name_is_explained_by_registry_and_dba_filing(case_3001):
    """case_4201's story: the merchant entered its DBA as the legal name; the TIN matches the
    registered name of the entity at the submitted number, which filed that DBA."""
    from fakes import bank, dba_filing
    case_3001["application"]["legal_name"] = "Bluefin Supply"
    case_3001["flag_reasons"] = ["TIN_MISMATCH"]

    def records(task):
        return [registry_entity("OH-5582114", "HARWICK MARINE SUPPLY LLC", state="OH"),
                dba_filing("OH-G21", "BLUEFIN SUPPLY", "HARWICK MARINE SUPPLY LLC", "OH-5582114"),
                tin_match("case_3001", "Bluefin Supply", "Mismatch"),
                tin_match("case_3001", "HARWICK MARINE SUPPLY LLC", "Match")]

    def evidence(task):
        return [document("D1", "irs_147c", name="HARWICK MARINE SUPPLY LLC", ein_last4="4321", date="2026-10-28"),
                bank("HARWICK MARINE SUPPLY LLC")]
    outcomes, *_ = _run(case_3001, records, evidence)
    o = outcomes["EC2.tin_match"]
    assert o.status == "EXPLAINED" and "assumed name" in o.explanation
    assert decide(outcomes) == "clear"

    def records_without_dba(task):
        return [r for r in records(task) if r.source_type != "state_dba"]
    outcomes, *_ = _run(case_3001, records_without_dba, evidence)
    assert outcomes["EC2.tin_match"].status == "CONFLICT"     # no DBA link: could be someone else's EIN


def test_clean_website_is_not_reported_as_suspicious(case_1042):
    *_, facts, _ = _run(case_1042, records_1042, evidence_1042)
    from app.graph.facts import truthy
    assert facts.evidence("suspicious") and not any(truthy(f.value) for f in facts.evidence("suspicious"))


def test_withdrawn_dba_filing_is_a_conflict_the_llm_cannot_explain(case_1042):
    from fakes import dba_filing

    def records(task):
        recs = records_1042(task)
        recs[1] = dba_filing("IL-AN-2024-0115", "NORTHLINE CYCLES", "NORTHLINE BICYCLE WORKS LLC", "IL-0458821",
                             status="withdrawn")
        return recs
    asked = []
    always = lambda check, a, facts: asked.append(check.check_id) or Explanation("trust me", ["R1"])
    outcomes, *_ = _run(case_1042, records, evidence_1042, explainer=always)
    o = outcomes["EC1.dba_owner"]
    assert o.status == "CONFLICT" and "withdrawn" in o.records_value
    assert "EC1.dba_owner" not in asked                     # hard data: never sent to the explainer
    assert decide(outcomes) == "follow_up"


def test_a_live_filing_beside_an_expired_one_still_ties_the_dba(case_1042):
    from fakes import dba_filing

    def records(task):
        recs = records_1042(task)
        recs[1] = dba_filing("IL-AN-2019-0007", "NORTHLINE CYCLES", "SOMEONE ELSE LLC", status="expired")
        recs.insert(2, dba_filing("IL-AN-2024-0115", "NORTHLINE CYCLES", "NORTHLINE BICYCLE WORKS LLC", "IL-0458821"))
        return recs
    outcomes, *_ = _run(case_1042, records, evidence_1042)
    assert outcomes["EC1.dba_owner"].status == "AGREE"
