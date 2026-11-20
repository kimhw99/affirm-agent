"""The Lead with the real BEAC-7 Records and Evidence workers, end to end, on the golden cases.

Everything is real except the chat model: the graph, the worker loop (``app.workers``),
the tools (``app.tools``), and the case data, read by ``FakeCaseClient`` from
``tests/fixtures/<ec>/<case_id>/`` (``case.json`` + ``world.json``). The chat model is a
deterministic stand-in (``PlaybookChatModel``) that picks tools the way the workers'
system prompts tell them to (``app/workers/prompts.py``), so the tests run offline.

The workers and the fixtures come from BEAC-7; without them (the agent branch on its
own) this module is skipped.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("app.workers.base", reason="the BEAC-7 workers are not in this tree")
pytest.importorskip("app.clients.case_client", reason="the BEAC-7 case client is not in this tree")

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app.clients.case_client import FakeCaseClient  # noqa: E402
from app.graph.llm import NullLeadLLM  # noqa: E402
from app.graph.worker_adapter import LangGraphWorker, build_workers  # noqa: E402
from app.graph.workflow import Deps, build_graph  # noqa: E402
from app.workers import WorkerSummary, run_evidence_worker, run_records_worker  # noqa: E402
from kyb_contracts.memo import CheckedMemo  # noqa: E402
from app.review import ReviewOutput  # noqa: E402
from kyb_contracts.worker_io import WorkerInput  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CASES = {p.parent.name: p.parent for p in sorted(FIXTURES.rglob("case.json"))}
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)     # the fixtures are written for this day
_ids = count(1)


# ---------- A chat model that follows the workers' playbooks ----------

def _call(tool: str, /, **args: Any) -> dict[str, Any]:
    return {"name": tool, "args": {k: v for k, v in args.items() if v is not None}, "id": f"call_{next(_ids)}"}


def _seen(messages) -> list[dict[str, Any]]:
    """The findings the tools returned so far (their trusted part, which is all a Records reply has)."""
    out = []
    for m in messages:
        if isinstance(m, ToolMessage):
            try:
                head = json.loads(m.content.split("\n", 1)[0])
            except ValueError:
                continue
            out += [{**f, "tool": head.get("tool")} for f in head.get("findings", [])]
    return out


class PlaybookChatModel:
    """bind_tools + with_structured_output, like ChatOpenAI, for one worker task."""

    def __init__(self, task: WorkerInput):
        self.task = task
        self.turn = 0
        self.calls: list[dict[str, Any]] = []

    def bind_tools(self, tools, **_):
        self.available = {t["function"]["name"] for t in tools}
        return self

    def with_structured_output(self, schema, **_):
        assert schema is WorkerSummary
        return self._Summary()

    class _Summary:
        def invoke(self, messages, config=None):
            return WorkerSummary(answer="The findings are listed.", hypothesis_signals=[], not_found=[],
                                 unresolved=[])

    def invoke(self, messages, config=None):
        self.turn += 1
        plan = self._plan(_seen(messages))
        calls = [c for c in plan if c["name"] in self.available]
        self.calls += calls
        return AIMessage(content="", tool_calls=calls) if calls else AIMessage(content="I have enough to answer.")

    # ----- what to call -----

    def _plan(self, seen: list[dict[str, Any]]) -> list[dict[str, Any]]:
        t = self.task
        if t.follow_up_of:
            return self._follow_up() if self.turn == 1 else []
        if t.worker == "evidence":
            return self._evidence_all() if self.turn == 1 else []
        if self.turn == 1:
            return self._records_first()
        if self.turn == 2:
            return self._tin_candidates(seen)
        return []

    def _evidence_all(self) -> list[dict[str, Any]]:
        ctx = self.task.context
        calls = [_call("get_document", doc_id=d.id) for d in ctx.documents]
        if ctx.bank_verification:
            calls.append(_call("get_bank_verification", bank_id=ctx.bank_verification.id))
        return calls + [_call("get_site_identity", web_id=w.id) for w in ctx.web_snapshot]

    def _records_first(self) -> list[dict[str, Any]]:
        ctx, q = self.task.context, self.task.question
        app = ctx.application
        sole = app.entity_type == "SOLE_PROPRIETORSHIP"
        calls = []
        if app.state_entity_id:
            calls += [_call("get_registry_entity"), _call("get_filing_history")]
        if not app.state_entity_id or "applied legal name" in q:
            calls.append(_call("search_registry", name=app.legal_name))
        if app.dba or "assumed-name" in q or sole:
            calls.append(_call("search_dba", name=app.dba or app.legal_name))
        if app.tin_last4:
            calls.append(_call("check_tin_match", name=app.legal_name))
        if "owner KYC" in q:
            calls += [_call("get_kyc_result", owner_ref=k.owner_ref) for k in ctx.owner_kyc]
        if "commercial" in q:
            calls.append(_call("search_commercial_db"))
        return calls

    def _tin_candidates(self, seen: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Prompt, EC-2: on a Mismatch, test the other candidate names, one call each."""
        app = self.task.context.application
        results = [f for f in seen if f["field"] == "tin_match_result"]
        if not results or any(f["value"] == "Match" for f in results) \
                or all(f["value"] in ("Not Issued", "Not Found") for f in results):
            return []
        names: list[str] = []
        for f in seen:
            if f["field"] == "former_name":
                names.append(f["value"])
            elif f["field"] == "former_names":
                names += list(f["value"])
            elif f["field"] == "legal_name" and f["tool"] == "get_registry_entity":
                names.append(f["value"])          # the registered name of the entity at the submitted number
        if app.dba:
            names.append(app.dba)
        if app.tin_type == "SSN" or app.entity_type == "SOLE_PROPRIETORSHIP":
            names += [o.name for o in app.owners]
        tested = {app.legal_name.casefold()}
        out = []
        for n in names:
            if n and n.casefold() not in tested:
                tested.add(n.casefold())
                out.append(_call("check_tin_match", name=n))
        return out

    def _follow_up(self) -> list[dict[str, Any]]:
        """Parse the Lead's targeted ask (app/graph/followup.py) into the matching tool calls."""
        q, ctx = self.task.question, self.task.context
        app = ctx.application
        if self.task.worker == "evidence":
            if "IRS letters" in q:
                return [_call("get_document", doc_id=d.id) for d in ctx.documents
                        if d.type in ("irs_cp575", "irs_147c", "w9")]
            if "bank verification status" in q:
                calls = [_call("get_bank_verification")] if ctx.bank_verification else []
                calls += [_call("get_document", doc_id=d.id) for d in ctx.documents if d.type == "business_license"]
                return calls + [_call("get_site_identity", web_id=w.id) for w in ctx.web_snapshot]
            return self._evidence_all()
        if m := re.search(r"TIN match again with the name '(.+?)'", q):
            return [_call("check_tin_match", name=m.group(1))]
        if m := re.search(r"KYC result for (owner:P\d+) and run the TIN match with the owner's name '(.+?)'", q):
            return [_call("get_kyc_result", owner_ref=m.group(1)), _call("check_tin_match", name=m.group(2))]
        if m := re.search(r"registry by the DBA '(.+?)'", q):
            return [_call("search_registry", name=m.group(1))] + \
                ([_call("get_registry_entity")] if app.state_entity_id else [])
        if m := re.search(r"is '(.+?)' a former name of it, or a separate company", q):
            return ([_call("get_filing_history")] if app.state_entity_id else []) + \
                [_call("search_registry", name=m.group(1))]
        if m := re.search(r"county-level DBA records in \w\w for '(.+?)'", q):
            return [_call("search_dba", name=m.group(1))]
        if m := re.search(r"Search the \w\w registry for '(.+?)' and read its filing history", q):
            return [_call("get_filing_history")] if app.state_entity_id else [_call("search_registry", name=m.group(1))]
        return []


class PlaybookWorker(LangGraphWorker):
    """The production adapter, with a playbook model per task instead of ChatOpenAI."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.models: list[PlaybookChatModel] = []

    def llm_for(self, task: WorkerInput) -> PlaybookChatModel:
        model = PlaybookChatModel(task)
        self.models.append(model)
        return model


# ---------- Running a review ----------

def _load(case_id: str) -> dict[str, Any]:
    return json.loads((CASES[case_id] / "case.json").read_text(encoding="utf-8"))


def _review(case_id: str):
    client = FakeCaseClient(CASES.values())
    clock = lambda: NOW  # noqa: E731
    records = PlaybookWorker("records", run_records_worker, client, clock=clock)
    evidence = PlaybookWorker("evidence", run_evidence_worker, client, clock=clock)
    sink: list = []
    graph = build_graph(Deps(records, evidence, llm=NullLeadLLM(), clock=clock, result_sink=sink.append))
    cfg = {"configurable": {"thread_id": case_id}}
    state = graph.invoke({"review_id": f"rv_{case_id}", "raw_case": _load(case_id)}, cfg)
    out = ReviewOutput.model_validate(state["review_output"])
    if out.checked_memo is not None:
        CheckedMemo.model_validate(state["review_output"]["checked_memo"])
    return graph, cfg, state, out, sink, records, evidence


def _status(out: ReviewOutput) -> dict[str, str]:
    return {c.check_id: c.status for c in out.lead.checks}


def _worker_results(state):
    return [r["result"] for r in state.get("worker_results", [])]


# ---------- The golden cases ----------

EXPECTED = {
    "case_1042": "recommend_clear",        # renamed LLC: the IRS and the DBA filing have the former name
    "case_4402": "recommend_clear",        # sole proprietor; hidden instructions on the website
    "case_4101": "recommend_clear",        # suffix and '&' only: intake resolves it
    "case_4102": "recommend_clear",        # DBA filed at the county by the LLC
    "case_4103": "recommend_clear",        # a dissolved look-alike entity in the registry
    "case_4201": "recommend_clear",        # the DBA was entered as the legal name
    "case_4202": "insufficient_evidence",  # the TIN matches none of the merchant's names
    "case_4401": "request_documents",      # brand-new LLC, EIN not issued yet
}


def test_every_fixture_has_an_expectation():
    assert set(EXPECTED) == set(CASES)


@pytest.mark.parametrize("case_id", sorted(EXPECTED))
def test_golden_case(case_id):
    graph, cfg, state, out, sink, records, evidence = _review(case_id)
    assert out.recommendation == EXPECTED[case_id], (_status(out), out.memo.notes_for_analyst)
    # The workers really ran (except when intake settled everything) and never failed.
    results = _worker_results(state)
    assert all(r["status"] != "failed" for r in results), [r["error"] for r in results]
    assert all(r["case_id"] == case_id for r in results)
    # A clear passes every step 9 check; R IDs never collide across parallel tasks.
    if out.recommendation == "recommend_clear":
        assert out.passed, (out.checked_memo.check_result, out.lead_check_failures)
    r_ids = [s["source_id"] for r in results for s in r["sources"] if s["source_id"].startswith("R")]
    assert len(r_ids) == len(set(r_ids))
    # The analyst decides and the result goes back to the backend.
    final = graph.invoke(Command(resume={"decision": "approve", "note": "ok", "analyst_id": "u_1"}), cfg)
    assert final["status"] == "done" and sink[0]["decision_record"]["case_id"] == case_id


def test_case_1042_reads_the_former_name_from_the_real_tools():
    *_, state, out, _, records, evidence = _review("case_1042")
    status = _status(out)
    assert status["EC2.tin_match"] == "EXPLAINED" and "former" in out.lead.check("EC2.tin_match").explanation
    assert status["EC1.dba_owner"] in ("AGREE", "EXPLAINED")
    assert out.memo.auto_resolved == ["NAME_SIMILAR_MATCH"] and out.memo.out_of_scope == ["PRODUCT_CATEGORY_REVIEW"]
    # The records worker tested the former name it found, and the IRS matched it.
    tested = [c["args"]["name"] for m in records.models for c in m.calls if c["name"] == "check_tin_match"]
    assert "HARBOR LIGHT CYCLES LLC" in tested
    assert len(evidence.models) == 1              # the merchant's side was read once


def test_case_4103_look_alike_is_not_the_applicant():
    *_, out, _, _, _ = _review("case_4103")
    assert not any(c.status == "HARD_FAIL" for c in out.lead.checks)
    assert _status(out)["EC1.entity_active"] == "AGREE"


def test_case_4201_dba_entered_as_legal_name_is_explained():
    *_, out, _, _, _ = _review("case_4201")
    tin = out.lead.check("EC2.tin_match")
    assert tin.status == "EXPLAINED" and "assumed name" in tin.explanation
    assert all(f.status == "resolved" for f in out.memo.flags)


def test_case_4402_hidden_instructions_are_reported_not_followed():
    *_, out, _, _, _ = _review("case_4402")
    assert "W1" in out.memo.notes_for_analyst and "not followed" in out.memo.notes_for_analyst
    assert out.lead.check("EC4.registry_presence").status == "EXPLAINED"     # expected: sole proprietor


def test_case_4401_new_ein_cannot_clear_and_asks_for_the_147c():
    *_, out, _, records, _ = _review("case_4401")
    assert [r.type for r in out.memo.document_request] == ["irs_147c"]       # it already has the CP 575
    assert out.lead.check("EC4.ein_age").status == "GAP"
    assert any(f.target == "records" for f in out.lead.follow_ups)            # an internal retry came first
    assert not any(f.status == "resolved" and f.flag == "TIN_NOT_ISSUED" for f in out.memo.flags)


def test_case_4101_formatting_only_skips_the_workers():
    *_, state, out, _, records, evidence = _review("case_4101")
    assert out.memo.auto_resolved == ["NAME_SIMILAR_MATCH"] and not out.memo.flags
    assert records.models == [] and evidence.models == []


def test_follow_ups_reach_only_the_affected_worker_with_their_own_r_ids():
    *_, state, out, _, records, evidence = _review("case_4202")
    follow_ups = [m.task for m in records.models if m.task.follow_up_of]
    assert follow_ups and all(t.question.startswith("[EC2.") for t in follow_ups)
    assert all(t.follow_up_of == "case_4202-records-1" for t in follow_ups)
    assert len(evidence.models) == 1


def test_service_wiring_uses_the_real_workers():
    client = FakeCaseClient(CASES.values())
    records, evidence = build_workers(client)
    assert isinstance(records, LangGraphWorker) and records.fn is run_records_worker
    assert isinstance(evidence, LangGraphWorker) and evidence.fn is run_evidence_worker
    placeholder, _ = build_workers(None, client_error="CASE_SERVICE_URL is not set")
    assert "CASE_SERVICE_URL" in placeholder.reason
