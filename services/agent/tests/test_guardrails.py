"""Step 9: evidence_index is built by code, and the hard rules catch bad memos."""

from fakes import evidence_1042, records_1042
from test_workflow import _review

from kyb_contracts.memo import CheckedMemo
from app.review import LeadReport

from app.graph.guardrails import run_checks
from app.graph.llm import NullLeadLLM
from app.graph.memo_writer import MemoProse
from app.graph.workflow import _case, _facts, _plan


def _setup(case_1042):
    graph, cfg, state, out, _, _ = _review(case_1042, records_1042, evidence_1042)
    case = _case(state)
    return out.memo, out.lead, case, _facts(state, case), _plan(state)


def test_index_excerpts_come_from_saved_data(case_1042):
    memo, report, case, facts, plan = _setup(case_1042)
    checked = run_checks(memo, report, case, facts, plan)
    assert checked.passed
    index = checked.evidence_index
    assert index["B1"].excerpt == "Account holder: NORTHLINE BICYCLE WORKS LLC DBA NORTHLINE CYCLES (verified)"
    assert index["D1"].type == "merchant_document"
    assert all(index[i].type == "official_record" for i in index if i.startswith("R"))
    # The memo's own contract accepts what step 9 built.
    CheckedMemo(memo=memo, evidence_index=index, check_result=checked.check_result)


def test_resolved_flag_needs_records_and_merchant_evidence(case_1042):
    memo, report, case, facts, plan = _setup(case_1042)
    flag = memo.flags[0]
    bad = memo.model_copy(update={"flags": [flag.model_copy(update={"evidence": [e for e in flag.evidence
                                                                                 if e[0] == "R"]})] + memo.flags[1:]})
    checked = run_checks(bad, report, case, facts, plan)
    assert [f.rule for f in checked.check_result.failures] == ["CLEAR_NEEDS_TWO_SOURCES"]


def test_unknown_citation_is_reported_with_its_id(case_1042):
    memo, report, case, facts, plan = _setup(case_1042)
    flag = memo.flags[0]
    bad = memo.model_copy(update={"flags": [flag.model_copy(update={"evidence": flag.evidence + ["R999"]})]
                                  + memo.flags[1:]})
    checked = run_checks(bad, report, case, facts, plan)
    missing = [f for f in checked.check_result.failures if f.rule == "CITED_ID_MUST_EXIST"]
    assert missing and missing[0].evidence == ["R999"] and "R999" not in checked.evidence_index
    CheckedMemo(memo=bad, evidence_index=checked.evidence_index, check_result=checked.check_result)


def test_missing_required_check_is_reported(case_1042):
    memo, report, case, facts, plan = _setup(case_1042)
    dropped = report.checks[-1].check_id
    bad = LeadReport.model_validate({
        **report.model_dump(),
        "checks": [c.model_dump() for c in report.checks[:-1]],
        "flag_checks": [{"flag": f.flag, "check_ids": [c for c in f.check_ids if c != dropped]}
                        for f in report.flag_checks],
    })
    checked = run_checks(memo, bad, case, facts, plan)
    assert "REQUIRED_CHECK_MISSING" in {f.rule for f in checked.lead_failures}
    assert not checked.passed


def test_document_already_in_the_case_is_reported(case_1042):
    from kyb_contracts.memo import DocumentRequest
    memo, report, case, facts, plan = _setup(case_1042)
    bad = memo.model_copy(update={"document_request": [DocumentRequest(type="irs_cp575", reason="x")]})
    checked = run_checks(bad, report, case, facts, plan)
    assert [f.rule for f in checked.lead_failures] == ["DOCUMENT_ALREADY_IN_CASE"]


def test_a_renewal_the_playbook_allows_is_not_reported(case_1042):
    """An expired DBA certificate is asked for again (followup.renewal_allowed); anything else isn't."""
    from kyb_contracts.case_input import Document
    from kyb_contracts.memo import DocumentRequest
    from app.review import DocumentCheck
    memo, report, case, facts, plan = _setup(case_1042)
    case = case.model_copy(update={"documents": case.documents + [Document.model_validate(
        {"id": "D9", "type": "dba_certificate", "confidence": 0.9, "extracted": {"dba": "Northline Cycles"}})]})
    checks = [c.model_copy(update={"status": "CONFLICT", "explanation": None, "explanation_evidence": [],
                                   "hard_fail_reason": None})
              if c.check_id == "EC1.dba_owner" else c for c in report.checks]
    asking = memo.model_copy(update={"document_request": [DocumentRequest(type="dba_certificate", reason="x")]})
    renewal = report.model_copy(update={"checks": checks, "document_checks": [
        DocumentCheck(type="dba_certificate", check_id="EC1.dba_owner")]})
    assert "DOCUMENT_ALREADY_IN_CASE" not in {f.rule for f in run_checks(asking, renewal, case, facts, plan).lead_failures}
    plain = report.model_copy(update={"checks": checks, "document_checks": []})
    assert "DOCUMENT_ALREADY_IN_CASE" in {f.rule for f in run_checks(asking, plain, case, facts, plan).lead_failures}


class LeakyLLM(NullLeadLLM):
    def memo_prose(self, payload, failures):
        return MemoProse(notes_for_analyst="Customer EIN is 12-3456789")


def test_memo_is_sent_back_once_then_goes_to_the_analyst_with_a_warning(case_1042):
    graph, cfg, state, out, _, _ = _review(case_1042, records_1042, evidence_1042, llm=LeakyLLM())
    assert "12-3456789" not in out.model_dump_json()                 # redacted, never shown
    assert not out.passed and not out.checked_memo.check_result.passed
    assert out.check_attempts == 2 and out.lead.version == 2         # one revision, no more
    assert {f.rule for f in out.checked_memo.check_result.failures} == {"NO_FULL_TAX_ID"}
    assert "human_review" in graph.get_state(cfg).next              # still reaches the analyst


# ---------- PR 4: what the memo cites and how it words a missing record ----------

def test_each_reviewed_flag_cites_the_supporting_records_its_checks_retrieved(case_1042):
    from app.graph.reconcile import tin_results
    memo, report, case, facts, plan = _setup(case_1042)
    for f in memo.flags:
        assert "K1" in f.evidence                       # owner KYC: retrieved, so the analyst sees it
    tin = next(f for f in memo.flags if f.flag == "TIN_MISMATCH")
    mismatch = next(r.fact.source_id for r in tin_results(facts, case) if r.result == "Mismatch")
    assert mismatch in tin.evidence                     # the failed match on the applied name, too


def test_claiming_the_business_does_not_exist_is_sent_back(case_1042):
    memo, report, case, facts, plan = _setup(case_1042)
    rules = lambda m: {f.rule for f in run_checks(m, report, case, facts, plan).lead_failures}
    for bad in ("The business does not exist.", "The business is not currently registered in Minnesota.",
                "The company appears unregistered.", "The entity does not appear to exist or operate."):
        assert "UNSUPPORTED_EXISTENCE_CLAIM" in rules(memo.model_copy(update={"notes_for_analyst": bad}))
    for fine in ("Northline Bicycle Works LLC is registered in IL; no registry record at IL-0000000.",
                 "The business's DBA 'Rushmere Tile Studio' is not officially registered.",   # golden 1127: true
                 "The EIN is not registered to the business name."):
        assert "UNSUPPORTED_EXISTENCE_CLAIM" not in rules(memo.model_copy(update={"notes_for_analyst": fine}))
