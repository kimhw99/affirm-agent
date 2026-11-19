"""Both workers end to end with a scripted fake chat model (bind_tools + with_structured_output)."""

from __future__ import annotations

import json
from itertools import count

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from helpers import NOW, TIN_LIKE, fake_client, task

from kyb_contracts.worker_io import WorkerResult

from app.clients.case_client import CaseServiceError
from app.tools import EVIDENCE_TOOLS, RECORDS_TOOLS
from app.tools._common import UNTRUSTED_CLOSE, UNTRUSTED_OPEN
from app.workers import WorkerSummary, run_evidence_worker, run_records_worker
from app.workers.base import RECOMMENDATION_REMOVED, strip_recommendation

_ids = count(1)


def calls(*pairs) -> AIMessage:
    """One model reply asking for tools: calls(("get_document", {"doc_id": "D1"}), ...)."""
    return AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"call_{next(_ids)}"} for n, a in pairs])


def summary(answer="The records answer the question (f_rec_t1_1).", signals=(), not_found=(), unresolved=()):
    return WorkerSummary(answer=answer, hypothesis_signals=list(signals), not_found=list(not_found),
                         unresolved=list(unresolved))


class ScriptedLLM:
    """Plays back scripted replies. A step may be an AIMessage or an exception to raise."""

    def __init__(self, steps=(), result=None):
        self.steps = list(steps)
        self.result = result if result is not None else summary()
        self.agent_calls: list[list] = []
        self.summary_calls: list[list] = []
        self.configs: list[dict] = []
        self.bound: list[str] = []
        self.structured_kwargs: dict = {}

    def bind_tools(self, tools, **_):
        self.bound = [t["function"]["name"] for t in tools]
        return _Bound(self)

    def with_structured_output(self, schema, **kwargs):
        assert schema is WorkerSummary
        self.structured_kwargs = kwargs
        return _Structured(self)


class _Bound:
    def __init__(self, llm):
        self.llm = llm

    def invoke(self, messages, config=None):
        self.llm.agent_calls.append(list(messages))
        self.llm.configs.append(config or {})
        step = self.llm.steps.pop(0) if self.llm.steps else AIMessage(content="I have enough to answer.")
        if isinstance(step, Exception):
            raise step
        return step


class _Structured:
    def __init__(self, llm):
        self.llm = llm

    def invoke(self, messages, config=None):
        self.llm.summary_calls.append(list(messages))
        result = self.llm.result
        if isinstance(result, Exception):
            raise result
        return result


def tool_messages(llm: ScriptedLLM) -> list[ToolMessage]:
    final = llm.agent_calls[-1] if llm.agent_calls else []
    return [m for m in final if isinstance(m, ToolMessage)]


def finding(result: WorkerResult, field: str, **match):
    return [f for f in result.findings if f.field == field and all(getattr(f, k) == v for k, v in match.items())]


def check_valid(result: WorkerResult) -> None:
    WorkerResult.model_validate(result.model_dump())
    assert not TIN_LIKE.search(result.model_dump_json())


# ---------------------------------------------------------------- happy paths

def test_records_happy_path_case_1042():
    llm = ScriptedLLM(
        steps=[
            calls(("get_registry_entity", {})),
            calls(("get_filing_history", {})),
            calls(("search_dba", {"name": "Northline Cycles"})),
            calls(("check_tin_match", {"name": "Northline Bicycle Works LLC"}),
                  ("check_tin_match", {"name": "HARBOR LIGHT CYCLES LLC"})),
            calls(("get_kyc_result", {"owner_ref": "owner:P1"})),
        ],
        result=summary(
            answer="IL-0458821 was renamed from HARBOR LIGHT CYCLES LLC (f_rec_t1_12), and the EIN matches only "
                   "the old name.",
            signals=[{"hypothesis_id": "h1", "direction": "supports", "finding_ids": ["f_rec_t1_12", "f_rec_t1_999"]},
                     {"hypothesis_id": "h9", "direction": "supports", "finding_ids": ["f_rec_t1_1"]},
                     {"hypothesis_id": "h2", "direction": "contradicts", "finding_ids": ["f_nope"]}]),
    )
    result = run_records_worker(task("case_1042", "records"), fake_client(), llm=llm, now=NOW)

    check_valid(result)
    assert result.status == "completed" and result.error is None
    assert result.tool_calls_used == 6
    assert [s.source_id for s in result.sources] == ["R1", "R2", "R3", "R4", "R5", "K1"]
    assert finding(result, "entity_status", value="active")
    assert finding(result, "filing_event")[1].value["effective_date"] == "2025-03-12"
    assert finding(result, "registrant", value="HARBOR LIGHT CYCLES LLC")
    assert [f.value for f in finding(result, "tin_match_result")] == ["Mismatch", "Match"]
    assert finding(result, "kyc_result", value="pass", subject="owner:P1")
    # Signals: unknown finding ids dropped, unknown hypotheses dropped, empty signals dropped.
    assert [s.model_dump() for s in result.hypothesis_signals] == [
        {"hypothesis_id": "h1", "direction": "supports", "finding_ids": ["f_rec_t1_12"]}]
    assert llm.bound == list(RECORDS_TOOLS)
    assert llm.structured_kwargs == {"method": "json_schema", "strict": True}


def test_evidence_happy_path_case_1042():
    llm = ScriptedLLM(steps=[
        calls(("get_document", {"doc_id": "D1"})),
        calls(("get_bank_verification", {"bank_id": "B1"}), ("get_site_identity", {"web_id": "W1"})),
    ], result=summary(answer="The CP 575 names HARBOR LIGHT CYCLES LLC (f_ev_t2_1)."))
    result = run_evidence_worker(task("case_1042", "evidence"), fake_client(), llm=llm, now=NOW)

    check_valid(result)
    assert result.status == "completed"
    assert [s.source_id for s in result.sources] == ["D1", "B1", "W1"]
    assert finding(result, "name", value="HARBOR LIGHT CYCLES LLC")
    assert finding(result, "tin_last4_matches_application", value=True)
    assert finding(result, "account_legal_name", value="NORTHLINE BICYCLE WORKS LLC")
    assert finding(result, "account_dba", value="NORTHLINE CYCLES")
    assert all(f.confidence == "low" for f in finding(result, "website_legal_name"))
    assert llm.bound == list(EVIDENCE_TOOLS)


def test_sole_proprietor_records_case_4402():
    llm = ScriptedLLM(steps=[
        calls(("search_registry", {"name": "Sunshine Cafe"})),
        calls(("search_dba", {"name": "Sunshine Cafe", "county": "Nassau"})),
        calls(("check_tin_match", {"name": "Sunshine Cafe"})),
        calls(("check_tin_match", {"name": "Mark Hemlock"})),
        calls(("get_kyc_result", {})),
    ])
    result = run_records_worker(task("case_4402", "records"), fake_client(), llm=llm, now=NOW)
    check_valid(result)
    assert finding(result, "registry_match", value="none")
    assert finding(result, "registrant", value="MARK HEMLOCK")
    assert finding(result, "tin_match_result", value="Match", subject="owner:P1")
    assert finding(result, "tin_type", value="SSN")
    assert finding(result, "kyc_result", value="pass")


def test_the_task_shows_only_this_workers_evidence_ids():
    rec, ev = ScriptedLLM(), ScriptedLLM()
    run_records_worker(task("case_1042", "records"), fake_client(), llm=rec, now=NOW)
    run_evidence_worker(task("case_1042", "evidence"), fake_client(), llm=ev, now=NOW)
    rec_task = next(m.content for m in rec.agent_calls[0] if isinstance(m, HumanMessage))
    ev_task = next(m.content for m in ev.agent_calls[0] if isinstance(m, HumanMessage))
    assert "K1: owner KYC" in rec_task and "D1:" not in rec_task and "B1:" not in rec_task
    assert "D1: document (irs_cp575)" in ev_task and "B1: bank" in ev_task and "K1" not in ev_task


# ---------------------------------------------------------------- permissions, budget, arguments

def test_wrong_worker_tool_is_rejected_and_counted():
    llm = ScriptedLLM(steps=[calls(("get_document", {"doc_id": "D1"}))])
    result = run_records_worker(task("case_1042", "records"), fake_client(), llm=llm, now=NOW)
    check_valid(result)
    assert result.tool_calls_used == 1
    assert result.sources == [] and result.findings == []
    reply = tool_messages(llm)[0].content
    assert "not available to the records worker" in reply


def test_budget_stops_the_loop():
    endless = [calls(("check_tin_match", {"name": f"Name {i}"})) for i in range(10)]
    llm = ScriptedLLM(steps=endless, result=summary(unresolved=["Did not test the DBA"]))
    result = run_records_worker(task("case_1042", "records", max_tool_calls=3), fake_client(), llm=llm, now=NOW)
    check_valid(result)
    assert result.tool_calls_used == 3
    assert len(llm.agent_calls) == 3          # no model call after the budget is used
    assert len(llm.summary_calls) == 1
    assert "budget ran out" in llm.summary_calls[0][-1].content
    assert result.status == "partial"          # budget used up with questions still open


def test_parallel_calls_past_the_budget_are_not_run():
    llm = ScriptedLLM(steps=[calls(("check_tin_match", {"name": "A"}), ("check_tin_match", {"name": "B"}),
                                   ("check_tin_match", {"name": "C"}))])
    result = run_records_worker(task("case_1042", "records", max_tool_calls=2), fake_client(), llm=llm, now=NOW)
    assert result.tool_calls_used == 2
    assert [s.source_id for s in result.sources] == ["R1", "R2"]
    assert result.status == "completed"        # budget used, but nothing left open


def test_invalid_arguments_are_reported_to_the_model():
    llm = ScriptedLLM(steps=[calls(("search_dba", {"county": "Cook"})), calls(("get_kyc_result", {}))])
    result = run_records_worker(task("case_1042", "records"), fake_client(), llm=llm, now=NOW)
    check_valid(result)
    assert result.status == "completed" and result.tool_calls_used == 2
    assert "invalid arguments" in tool_messages(llm)[0].content
    assert [s.source_id for s in result.sources] == ["K1"]


# ---------------------------------------------------------------- failures never raise

class FailingClient:
    """The fixtures' fake client, except one method raises."""

    def __init__(self, method, error):
        self.inner, self.method, self.error = fake_client(), method, error

    def __getattr__(self, name):
        if name == self.method:
            def boom(*a, **k):
                raise self.error
            return boom
        return getattr(self.inner, name)


def test_case_service_error_gives_partial():
    llm = ScriptedLLM(steps=[calls(("get_registry_entity", {})), calls(("get_kyc_result", {}))])
    client = FailingClient("get_registry_entity", CaseServiceError("GET /registry/...: HTTP 503"))
    result = run_records_worker(task("case_1042", "records"), client, llm=llm, now=NOW)
    check_valid(result)
    assert result.status == "partial"
    assert "case service error" in result.error
    assert [s.source_id for s in result.sources] == ["K1"]


def test_tool_crash_gives_partial():
    llm = ScriptedLLM(steps=[calls(("get_document", {"doc_id": "D1"}))])
    result = run_evidence_worker(task("case_1042", "evidence"), FailingClient("get_document", KeyError("x")),
                                 llm=llm, now=NOW)
    check_valid(result)
    assert result.status == "partial" and "KeyError" in result.error


def test_llm_error_gives_failed_and_keeps_findings():
    llm = ScriptedLLM(steps=[calls(("get_kyc_result", {})), TimeoutError("OpenAI timed out")])
    result = run_records_worker(task("case_1042", "records"), fake_client(), llm=llm, now=NOW)
    check_valid(result)
    assert result.status == "failed"
    assert "LLM call failed" in result.error and "TimeoutError" in result.error
    assert [s.source_id for s in result.sources] == ["K1"]
    assert result.answer is None and llm.summary_calls == []


def test_summary_error_gives_partial():
    llm = ScriptedLLM(steps=[calls(("get_kyc_result", {}))], result=ValueError("bad JSON"))
    result = run_records_worker(task("case_1042", "records"), fake_client(), llm=llm, now=NOW)
    check_valid(result)
    assert result.status == "partial" and "summary failed" in result.error
    assert result.findings and result.answer is None


def test_graph_setup_failure_gives_failed():
    class Broken(ScriptedLLM):
        def bind_tools(self, tools, **_):
            raise RuntimeError("no tools for you")

    result = run_records_worker(task("case_1042", "records"), fake_client(), llm=Broken(), now=NOW)
    check_valid(result)
    assert result.status == "failed" and "RuntimeError" in result.error


def test_missing_model_config_gives_failed(monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    result = run_records_worker(task("case_1042", "records"), fake_client(), now=NOW)
    check_valid(result)
    assert result.status == "failed" and "OPENAI_MODEL" in result.error


# ---------------------------------------------------------------- untrusted content, recommendations

def test_injection_in_website_values_is_flagged_wrapped_and_not_followed():
    llm = ScriptedLLM(
        steps=[calls(("get_site_identity", {"web_id": "W1"})), calls(("get_document", {"doc_id": "D1"}))],
        result=summary(answer="The website legal name is Sunshine Cafe (f_ev_t2_1). Its DBA field holds "
                              "instruction-like text (f_ev_t2_6). Based on this I recommend approval."),
    )
    result = run_evidence_worker(task("case_4402", "evidence"), fake_client(), llm=llm, now=NOW)
    check_valid(result)

    assert finding(result, "injection_suspected", value=True)
    # The worker kept going with its own plan after reading the injected text.
    assert [s.source_id for s in result.sources] == ["W1", "D1"]
    # The injected text reached the model only inside untrusted tags, in the loop and in the summary.
    site_reply = next(m.content for m in llm.agent_calls[1] if isinstance(m, ToolMessage))
    outside = site_reply.split(UNTRUSTED_OPEN)[0] + site_reply.split(UNTRUSTED_CLOSE)[-1]
    assert "Ignore all previous instructions" in site_reply and "Ignore all previous" not in outside
    summary_prompt = llm.summary_calls[0][-1].content
    assert summary_prompt.index("Ignore all previous") > summary_prompt.index(UNTRUSTED_OPEN)
    # The recommendation sentence is stripped and noted.
    assert "recommend" not in result.answer.lower()
    assert RECOMMENDATION_REMOVED in result.unresolved


@pytest.mark.parametrize("answer, expected, removed", [
    ("The EIN matches the old name (f1).", "The EIN matches the old name (f1).", False),
    ("The EIN matches (f1). We recommend approving the merchant.", "The EIN matches (f1).", True),
    ("Deny this case.", None, True),
    ("The entity is active (f2). The analyst should decline it! Bank verified (f3).",
     "The entity is active (f2). Bank verified (f3).", True),
])
def test_strip_recommendation(answer, expected, removed):
    assert strip_recommendation(answer) == (expected, removed)


def test_config_metadata_reaches_the_model_calls():
    llm = ScriptedLLM()
    run_records_worker(task("case_1042", "records"), fake_client(), llm=llm, now=NOW,
                       config={"metadata": {"review_id": "rv1"}, "tags": ["lead"]})
    config = llm.configs[0]
    assert config["metadata"]["review_id"] == "rv1"
    assert {config["metadata"][k] for k in ("case_id", "task_id", "worker")} == {"case_1042", "t1", "records"}


def test_follow_up_uses_record_id_start_and_prior_findings():
    first = run_records_worker(task("case_1042", "records"), fake_client(),
                               llm=ScriptedLLM(steps=[calls(("get_filing_history", {}))]), now=NOW)
    prior = [f for f in first.findings if f.field == "former_name"]
    llm = ScriptedLLM(steps=[calls(("search_dba", {"name": "Northline Cycles"}))])
    follow = run_records_worker(
        task("case_1042", "records", task_id="t3", follow_up_of="t1", record_id_start=2,
             prior_findings=[f.model_dump() for f in prior]),
        fake_client(), llm=llm, now=NOW)
    check_valid(follow)
    assert [s.source_id for s in follow.sources] == ["R2"]
    assert all(f.finding_id.startswith("f_rec_t3_") for f in follow.findings)
    # The former name from the prior task is used in the registrant comparison.
    assert any(f.field == "name_comparison" and f.value["against"] == "former_name" and f.value["label"] == "exact"
               for f in follow.findings)
    assert "Prior findings" in next(m.content for m in llm.agent_calls[0] if isinstance(m, HumanMessage))


@pytest.mark.parametrize("case_id", ["case_4101", "case_4102", "case_4103", "case_4201", "case_4202", "case_4401"])
def test_every_fixture_runs_both_workers_to_a_valid_result(case_id):
    rec_steps = [calls(("get_registry_entity", {})), calls(("check_tin_match", {"name": "Test Name"})),
                 calls(("get_kyc_result", {}))]
    ev_steps = [calls(("get_document", {"doc_id": "D1"})), calls(("get_bank_verification", {})),
                calls(("get_site_identity", {}))]
    for runner, worker, steps in ((run_records_worker, "records", rec_steps),
                                  (run_evidence_worker, "evidence", ev_steps)):
        result = runner(task(case_id, worker), fake_client(), llm=ScriptedLLM(steps=steps), now=NOW)
        check_valid(result)
        assert result.status == "completed", result.error
        assert json.loads(result.model_dump_json())["worker"] == worker
