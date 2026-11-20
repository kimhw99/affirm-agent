"""The Lead reads the workers' own reports: not_found, unresolved, status/error, answer, signals."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from conftest import NOW, TODAY
from fakes import ScriptedWorker, evidence_1042, records_1042
from kyb_contracts.case_input import CaseInput
from kyb_contracts.worker_io import WorkerInput, WorkerResult
from app.review import CheckOutcome, ReviewOutput

from app.graph.facts import FactIndex
from app.graph.followup import next_follow_ups
from app.graph.llm import NullLeadLLM
from app.graph.orchestrator import route_case
from app.graph.plan import build_plan, initial_tasks
from app.graph.workflow import Deps, build_graph
from app.graph.worker_notes import WorkerNote, notes_by_check, worker_issues, worker_reports


# ---------- A scripted worker that also reports on itself ----------

@dataclass
class ReportingWorker:
    """ScriptedWorker plus what the summary step adds: not_found, unresolved, status, signals."""

    inner: ScriptedWorker
    report: Callable[[WorkerInput, WorkerResult], dict[str, Any]]

    @property
    def calls(self):
        return self.inner.calls

    def run(self, task: WorkerInput) -> WorkerResult:
        result = self.inner.run(task)
        return WorkerResult.model_validate({**result.model_dump(), **self.report(task, result)})


def _graph(raw, records, evidence, llm=None):
    deps = Deps(records, evidence, llm=llm or NullLeadLLM(), clock=lambda: NOW)
    graph = build_graph(deps)
    state = graph.invoke({"review_id": "rv_notes", "raw_case": raw}, {"configurable": {"thread_id": "n1"}})
    return state, ReviewOutput.model_validate(state["review_output"])


def _results(raw, records_report=None, evidence_report=None):
    d, case = route_case(raw)
    plan = build_plan(case, d, TODAY)
    out = []
    for t in initial_tasks(case, d, plan):
        script = records_1042 if t.worker == "records" else evidence_1042
        r = ScriptedWorker(t.worker, script).run(t)
        extra = (records_report if t.worker == "records" else evidence_report) or {}
        out.append(WorkerResult.model_validate({**r.model_dump(), **extra}))
    return case, plan, out


# ---------- Tying notes to checks ----------

def test_notes_are_tied_to_checks_by_focus_field_and_boilerplate_is_dropped(case_1042):
    _, plan, results = _results(case_1042, records_report={
        "unresolved": ["Did not run the TIN match with the CP 575 name; the budget ran out",
                       "No summary was produced; the findings are listed without interpretation."],
        "not_found": ["No assumed-name filing naming the DBA owner in Cook County",
                      "Something about the weather"],
    })
    per_check, loose = notes_by_check(plan, results)
    assert [n.text for n in per_check["EC2.tin_match"]] == [
        "Did not run the TIN match with the CP 575 name; the budget ran out"]
    assert [n.kind for n in per_check["EC1.dba_owner"]] == ["not_found"]
    assert [n.text for n in loose] == ["Something about the weather"]
    assert not any("No summary" in n.text for ns in per_check.values() for n in ns)


def test_a_follow_up_for_the_check_supersedes_the_first_pass(case_1042):
    case, plan, results = _results(case_1042, records_report={"unresolved": ["TIN match not run"]})
    follow = results[0].model_copy(update={"task_id": "case_1042-records-F1", "unresolved": [],
                                           "not_found": ["No other name matched the TIN"]})
    history = [{"task_id": "case_1042-records-F1", "check_id": "EC2.tin_match"}]
    per_check, _ = notes_by_check(plan, results + [follow], history)
    assert [(n.kind, n.text) for n in per_check["EC2.tin_match"]] == [("not_found", "No other name matched the TIN")]


# ---------- Choosing follow-ups ----------

def _o(cid, status, edge="EC-1"):
    return CheckOutcome(check_id=cid, edge_case=edge, description=cid, status=status, confidence="low")


def test_unresolved_notes_pick_the_worker_and_steps_are_tracked_by_name(case_1042):
    case = CaseInput.model_validate(case_1042)
    facts = FactIndex(case, [])
    outcomes = {"EC1.bank_account_holder": _o("EC1.bank_account_holder", "CONFLICT")}
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)

    # No notes: the playbook order (registry retry, a records step) comes first.
    assert next_follow_ups(case, outcomes, facts, [], 1, now)[0].step == "registry_retry"

    # The evidence worker said it couldn't settle the account holder: ask it first, and tell it why.
    notes = {"EC1.bank_account_holder": [
        WorkerNote("t2", "evidence", "unresolved", "The bank account holder's DBA part is cut off"),
        WorkerNote("t2", "evidence", "not_found", "No DBA certificate in the case")]}
    fu = next_follow_ups(case, outcomes, facts, [], 1, now, notes=notes)[0]
    assert (fu.target, fu.step) == ("evidence", "evidence_names")
    assert "still unresolved from your last pass: The bank account holder's DBA part is cut off" in fu.context
    assert "already searched with no record found: No DBA certificate in the case" in fu.context
    assert "evidence worker, still unresolved" in fu.reason

    # Next round: the step that ran is skipped by name, so the other one runs, then nothing is left.
    history = [{"check_id": fu.check_id, "target": fu.target, "step": fu.step, "task_id": fu.task_id}]
    fu2 = next_follow_ups(case, outcomes, facts, history, 2, now, notes=notes)[0]
    assert fu2.step == "registry_retry"
    history.append({"check_id": fu2.check_id, "target": fu2.target, "step": fu2.step, "task_id": fu2.task_id})
    assert next_follow_ups(case, outcomes, facts, history, 3, now, notes=notes) == []


# ---------- Worker status ----------

def test_failed_tasks_are_errors_and_partial_ones_warnings(case_1042):
    _, _, results = _results(case_1042)
    failed = results[0].model_copy(update={"status": "failed", "error": "LLM call failed"})
    partial = results[1].model_copy(update={"status": "partial", "error": "get_site_identity: case service error"})
    budget = results[1].model_copy(update={"task_id": "x", "status": "partial", "unresolved": ["DBA not checked"]})
    errors, warnings = worker_issues([failed, partial, budget])
    assert errors == ["case_1042-records-1: LLM call failed"]
    assert warnings == ["case_1042-evidence-1: get_site_identity: case service error", "x: tool budget used up"]


def test_worker_reports_give_hypothesis_text_and_evidence_ids(case_1042):
    _, plan, results = _results(case_1042)
    ev = results[1]
    signal = {"hypothesis_id": "h2", "direction": "supports", "finding_ids": [ev.findings[0].finding_id]}
    ev = WorkerResult.model_validate({**ev.model_dump(), "hypothesis_signals": [signal],
                                      "answer": "The CP 575 names a different company."})
    report = worker_reports([ev], plan)[0]
    assert report["answer"] == "The CP 575 names a different company."
    assert report["hypothesis_signals"] == [{
        "hypothesis": plan.hypotheses[1].text, "direction": "supports",
        "finding_keys": [f"{ev.task_id}:{ev.findings[0].finding_id}"], "evidence": ["D1"]}]


# ---------- End to end ----------

def test_follow_up_question_carries_what_the_worker_left_open(case_1042):
    def report(task, result):
        if task.follow_up_of is None:
            return {"unresolved": ["Did not run the TIN match with the CP 575 name; the budget ran out"],
                    "status": "partial"}
        return {}

    records = ReportingWorker(ScriptedWorker("records", records_1042), report)
    evidence = ScriptedWorker("evidence", evidence_1042)
    state, out = _graph(case_1042, records, evidence)
    assert out.memo.recommendation == "recommend_clear"
    tin = next(t for t in records.calls if t.question.startswith("[EC2.tin_match]"))
    assert "Context: still unresolved from your last pass: Did not run the TIN match" in tin.question
    fu = next(f for f in out.lead.follow_ups if f.check_id == "EC2.tin_match")
    assert fu.step == "tin_retry" and "records worker, still unresolved" in fu.reason
    # A partial task is shown to the analyst even on a clear.
    assert "Partial worker results" in out.memo.notes_for_analyst


def test_memo_says_why_a_check_is_still_open_and_lists_partial_workers(case_1042):
    def records_script(task):              # the follow-ups never find the name change
        return records_1042(task) if task.follow_up_of is None else []

    def records_report(task, result):
        if task.follow_up_of is None:
            return {}
        return {"not_found": ["No name change in the filing history of IL-0458821"]}

    def evidence_report(task, result):
        return {"status": "partial", "error": "get_site_identity: case service error: timeout"}

    records = ReportingWorker(ScriptedWorker("records", records_script), records_report)
    evidence = ReportingWorker(ScriptedWorker("evidence", evidence_1042), evidence_report)
    state, out = _graph(case_1042, records, evidence)
    memo = out.memo
    assert memo.recommendation in ("request_documents", "insufficient_evidence")
    open_ids = {c.check_id for c in out.lead.open_checks()}
    assert "EC2.tin_match" in open_ids
    notes = memo.notes_for_analyst
    assert "What the workers reported for the open checks" in notes
    assert "EC2.tin_match: records worker, not found: No name change in the filing history" in notes
    assert "Partial worker results" in notes and "case_1042-evidence-1" in notes
    # A partial worker is a warning: the merchant can still be asked for what would settle a check.
    assert "Worker failures" not in notes
    tin_flag = next(f for f in memo.flags if f.flag == "TIN_MISMATCH")
    assert tin_flag.status == "unresolved" and "No name change" in tin_flag.root_cause


class _CapturingLLM(NullLeadLLM):
    def __init__(self):
        self.payloads = []

    def memo_prose(self, payload, failures):
        self.payloads.append(payload)
        return None


def test_memo_llm_receives_each_workers_answer_and_signals(case_1042):
    def report(task, result):
        if task.follow_up_of is not None:
            return {}
        sig = {"hypothesis_id": "h1", "direction": "contradicts", "finding_ids": [result.findings[0].finding_id]}
        return {"answer": "The CP 575 names HARBOR LIGHT CYCLES LLC.", "hypothesis_signals": [sig]}

    llm = _CapturingLLM()
    evidence = ReportingWorker(ScriptedWorker("evidence", evidence_1042), report)
    _graph(case_1042, ScriptedWorker("records", records_1042), evidence, llm=llm)
    reports = llm.payloads[0]["worker_reports"]
    ev = next(r for r in reports if r["worker"] == "evidence")
    assert ev["answer"] == "The CP 575 names HARBOR LIGHT CYCLES LLC."
    assert ev["hypothesis_signals"][0]["direction"] == "contradicts"
    assert ev["hypothesis_signals"][0]["evidence"] == ["D1"]
