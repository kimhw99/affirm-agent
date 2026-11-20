"""Step 9: evidence_index is built by code, and the hard rules catch bad memos."""

from fakes import ScriptedWorker, evidence_1042, records_1042
from test_workflow import _review

from app.graph.guardrails import run_checks
from app.graph.llm import NullLeadLLM
from app.graph.memo_writer import MemoProse
from app.graph.workflow import _case, _facts, _plan


def _setup(case_1042):
    graph, cfg, state, out, _, _ = _review(case_1042, records_1042, evidence_1042)
    case = _case(state)
    return out.memo, case, _facts(state, case), _plan(state)


def test_index_excerpts_come_from_saved_data(case_1042):
    memo, case, facts, plan = _setup(case_1042)
    result, index = run_checks(memo, case, facts, plan)
    assert result.passed
    assert index["B1"].excerpt == "Account holder: NORTHLINE BICYCLE WORKS LLC DBA NORTHLINE CYCLES (verified)"
    assert index["D1"].type == "merchant_document" and index["D1"].authority == "merchant"
    assert all(index[i].type == "official_record" for i in index if i.startswith("R"))


def test_resolved_flag_needs_records_and_merchant_evidence(case_1042):
    memo, case, facts, plan = _setup(case_1042)
    flag = memo.flags[0]
    bad = memo.model_copy(update={"flags": [flag.model_copy(update={"evidence": [e for e in flag.evidence
                                                                                 if e[0] == "R"]})] + memo.flags[1:]})
    result, _ = run_checks(bad, case, facts, plan)
    assert [f.rule for f in result.failures] == ["CLEAR_NEEDS_TWO_SOURCES"]


def test_unknown_citation_and_existing_document_request(case_1042):
    memo, case, facts, plan = _setup(case_1042)
    check = memo.checks[0].model_copy(update={"evidence": memo.checks[0].evidence + ["R999"]})
    bad = memo.model_copy(update={"checks": [check] + memo.checks[1:]})
    result, index = run_checks(bad, case, facts, plan)
    assert "CITATION_NOT_FOUND" in {f.rule for f in result.failures} and "R999" not in index


def test_missing_required_check_is_reported(case_1042):
    memo, case, facts, plan = _setup(case_1042)
    bad = memo.model_copy(update={"checks": memo.checks[:-1], "flags": [
        f.model_copy(update={"check_ids": [c for c in f.check_ids if c != memo.checks[-1].check_id]})
        for f in memo.flags]})
    result, _ = run_checks(bad, case, facts, plan)
    assert "REQUIRED_CHECK_MISSING" in {f.rule for f in result.failures}


class LeakyLLM(NullLeadLLM):
    def memo_prose(self, payload, failures):
        return MemoProse(notes_for_analyst="Customer EIN is 12-3456789")


def test_memo_is_sent_back_once_then_goes_to_the_analyst_with_a_warning(case_1042):
    graph, cfg, state, out, _, _ = _review(case_1042, records_1042, evidence_1042, llm=LeakyLLM())
    assert "12-3456789" not in out.model_dump_json()                 # redacted, never shown
    assert not out.check_result.passed
    assert out.check_result.attempts == 2 and out.memo.version == 2  # one revision, no more
    assert {f.rule for f in out.check_result.failures} == {"FULL_TIN_IN_MEMO"}
    assert "human_review" in graph.get_state(cfg).next              # still reaches the analyst
