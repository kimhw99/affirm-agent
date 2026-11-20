"""The LangGraph workflow: orchestrator, Lead, workers, reconcile, follow-ups, memo, checks,
human review.

    intake ──(route)──> plan ──Send×N──> run_worker ──> reconcile ──> follow_up ─┐
       │                                     ^                                  │
       │                                     └──────── Send (one per check) ────┘
       └──(human_queue / needs_info)──────────────────────────> write_memo <──────┘
                                                                  │   ^
                                                               checks ┘ (sent back once)
                                                                  │
                                                            human_review (interrupt)
                                                                  │
                                                              finalize ──> backend API

Workers are injected (``Deps``): anything with ``run(WorkerInput) -> WorkerResult``.
``app.graph.worker_adapter.LangGraphWorker`` wraps the BEAC-7 Records and Evidence
workers (``app.workers``) that way. State holds only JSON-friendly values so any
LangGraph checkpointer can save it.
"""

from __future__ import annotations

import logging
import operator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any, Callable, Optional, Protocol, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from app.graph.facts import FactIndex
from app.graph.followup import (
    PendingFollowUp,
    follow_up_task,
    has_internal_candidates,
    next_follow_ups,
    safe_ask,
    stop_reason,
    to_record,
)
from app.graph.guardrails import run_checks
from app.graph.llm import LeadLLM, NullLeadLLM, finding_view, llm_explainer
from app.graph.memo_writer import MemoProse, Revision, build_memo
from app.graph.orchestrator import AutoResolved, RoutingDecision, route_case
from app.graph.plan import Plan, build_plan, initial_tasks
from app.graph.reconcile import reconcile
from kyb_contracts.case_input import CaseInput, FlagReason
from kyb_contracts.decision import STATUS_AFTER, DecisionInput, DecisionRecord, is_override
from kyb_contracts.memo import CheckedMemo, CheckResult, Memo
from app.review import CheckOutcome, FollowUpRecord, LeadCheckFailure, LeadReport, ReviewOutput
from kyb_contracts.worker_io import WorkerInput, WorkerResult

log = logging.getLogger(__name__)


# ---------- Dependencies ----------

class Worker(Protocol):
    def run(self, task: WorkerInput) -> WorkerResult: ...


ResultSink = Callable[[dict[str, Any]], None]


@dataclass
class Deps:
    records_worker: Worker
    evidence_worker: Worker
    llm: LeadLLM = field(default_factory=NullLeadLLM)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    result_sink: Optional[ResultSink] = None     # sends the final decision back to the backend API
    callbacks: list[Any] = field(default_factory=list)


# ---------- State ----------

class ReviewState(TypedDict, total=False):
    review_id: str
    raw_case: dict[str, Any]
    case: Optional[dict[str, Any]]
    routing: dict[str, Any]
    plan: Optional[dict[str, Any]]
    initial_task_ids: dict[str, str]
    worker_results: Annotated[list[dict[str, Any]], operator.add]   # {"round": n, "result": WorkerResult}
    outcomes: dict[str, dict[str, Any]]
    pending_follow_ups: list[dict[str, Any]]
    follow_up_history: list[dict[str, Any]]                          # FollowUpRecord dicts
    internal_rounds: int
    last_round_new: Optional[bool]
    stop_reason: Optional[str]
    memo: Optional[dict[str, Any]]                                   # memo.Memo; None without flags
    report: Optional[dict[str, Any]]                                 # review.LeadReport
    memo_attempts: int
    tin_redacted: bool
    check_result: Optional[dict[str, Any]]
    lead_check_failures: list[dict[str, Any]]
    evidence_index: dict[str, Any]
    review_output: Optional[dict[str, Any]]
    decision_record: Optional[dict[str, Any]]
    status: str


# ---------- Rehydration helpers ----------

def _routing(d: dict[str, Any]) -> RoutingDecision:
    return RoutingDecision(
        route=d["route"], reason=d.get("reason"), case_id=d.get("case_id"),
        open_flags=[FlagReason(f) for f in d.get("open_flags", [])],
        auto_resolved=[AutoResolved(FlagReason(a["flag"]), a["reason"]) for a in d.get("auto_resolved", [])],
        out_of_scope=[FlagReason(f) for f in d.get("out_of_scope", [])],
        edge_cases=d.get("edge_cases", []), records_tasks=d.get("records_tasks", []),
        evidence_tasks=d.get("evidence_tasks", []), missing_inputs=d.get("missing_inputs", []),
        normalized=d.get("normalized", {}),
    )


def _case(state: ReviewState) -> Optional[CaseInput]:
    return CaseInput.model_validate(state["case"]) if state.get("case") else None


def _plan(state: ReviewState) -> Optional[Plan]:
    return Plan.model_validate(state["plan"]) if state.get("plan") else None


def _results(state: ReviewState) -> list[tuple[int, WorkerResult]]:
    return [(r["round"], WorkerResult.model_validate(r["result"])) for r in state.get("worker_results", [])]


def _facts(state: ReviewState, case: CaseInput) -> FactIndex:
    return FactIndex(case, _results(state))


def _outcomes(state: ReviewState) -> dict[str, CheckOutcome]:
    return {k: CheckOutcome.model_validate(v) for k, v in (state.get("outcomes") or {}).items()}


def _dump(o) -> dict[str, Any]:
    return o.model_dump(mode="json")


def _failed_result(task: WorkerInput, error: str) -> WorkerResult:
    return WorkerResult(task_id=task.task_id, case_id=task.context.case_id, worker=task.worker,
                        status="failed", findings=[], sources=[], hypothesis_signals=[],
                        tool_calls_used=0, error=error[:500])


def _summary(case: CaseInput, routing: RoutingDecision) -> dict[str, Any]:
    """What the Lead plans from: the application, vendor labels, evidence keys. No document text."""
    return {
        "case_id": case.case_id,
        "open_flags": [f.value for f in routing.open_flags],
        "edge_cases": routing.edge_cases,
        "application": case.application.model_dump(mode="json"),
        "vendor_checks": case.checks.model_dump(mode="json"),
        "evidence_keys": {
            "documents": [{"id": d.id, "type": d.type} for d in case.documents],
            "bank_verification": case.bank_verification.id if case.bank_verification else None,
            "web_snapshot": [w.id for w in case.web_snapshot],
            "owner_kyc": [k.id for k in case.owner_kyc],
        },
        "normalized": routing.normalized,
    }


# ---------- Graph ----------

def build_graph(deps: Deps, checkpointer=None):
    explainer = llm_explainer(deps.llm)

    def today():
        return deps.clock().date()

    # --- 2. intake / orchestrator (code) ---
    def intake(state: ReviewState) -> dict[str, Any]:
        decision, case = route_case(state["raw_case"])
        return {
            "routing": decision.to_dict(),
            "case": case.model_dump(mode="json") if case else None,
            "internal_rounds": 0, "memo_attempts": 0, "follow_up_history": [],
            "pending_follow_ups": [], "outcomes": {}, "last_round_new": None, "status": "running",
        }

    def after_intake(state: ReviewState) -> str:
        r = state["routing"]
        return "plan" if r["route"] == "reviewed" and r["open_flags"] else "write_memo"

    # --- 3. Lead: plan ---
    def plan_node(state: ReviewState) -> dict[str, Any]:
        case = _case(state)
        routing = _routing(state["routing"])
        base = build_plan(case, routing, today())
        extras = deps.llm.plan_extras(_summary(case, routing),
                                      [c.model_dump(mode="json", include={"check_id", "description", "pass_rule"})
                                       for c in base.checks])
        plan = build_plan(case, routing, today(), extras) if extras else base
        tasks = initial_tasks(case, routing, plan)
        return {
            "plan": _dump(plan),
            "initial_task_ids": {t.worker: t.task_id for t in tasks},
            "pending_follow_ups": [],
        }

    def fan_out_initial(state: ReviewState):
        case = _case(state)
        routing = _routing(state["routing"])
        tasks = initial_tasks(case, routing, _plan(state))
        if not tasks:
            return "reconcile"
        return [Send("run_worker", {"task": _dump(t), "round": 0}) for t in tasks]

    # --- 4/5. workers (in parallel; injected) ---
    def run_worker(payload: dict[str, Any]) -> dict[str, Any]:
        task = WorkerInput.model_validate(payload["task"])
        worker = deps.records_worker if task.worker == "records" else deps.evidence_worker
        try:
            result = worker.run(task)
            result = WorkerResult.model_validate(result.model_dump() if isinstance(result, WorkerResult) else result)
            if result.task_id != task.task_id or result.worker != task.worker \
                    or result.case_id != task.context.case_id:
                result = _failed_result(task, "worker returned a result for a different task")
        except Exception as exc:  # noqa: BLE001 - a failed worker is a GAP, not a crash
            log.exception("worker %s failed", task.task_id)
            result = _failed_result(task, f"{type(exc).__name__}: {exc}")
        return {"worker_results": [{"round": payload["round"], "result": _dump(result)}]}

    # --- 6. reconcile (code; the LLM only judges EXPLAINED) ---
    def reconcile_node(state: ReviewState) -> dict[str, Any]:
        case, plan = _case(state), _plan(state)
        facts = _facts(state, case)
        pending = [PendingFollowUp.from_dict(p) for p in state.get("pending_follow_ups") or []]
        if not pending:
            outcomes, _ = reconcile(case, plan, facts, today(), explainer)
            return {"outcomes": {k: _dump(v) for k, v in outcomes.items()}}

        # A follow-up round: re-reconcile only the affected checks.
        round_no = state.get("internal_rounds", 0) + 1
        prev = _outcomes(state)
        outcomes, _ = reconcile(case, plan, facts, today(), explainer,
                                only=[p.check_id for p in pending], previous=prev)
        now = deps.clock()
        history = list(state.get("follow_up_history") or [])
        new_keys = facts.finding_keys_in_round(round_no)
        for p in pending:
            task_new = {facts.by_key(k).source_id for k in new_keys if k.startswith(p.task_id + ":")}
            history.append(_dump(to_record(p, outcomes[p.check_id].status, sorted(task_new), now)))
        return {
            "outcomes": {k: _dump(v) for k, v in outcomes.items()},
            "follow_up_history": history,
            "pending_follow_ups": [],
            "internal_rounds": round_no,
            "last_round_new": bool(new_keys),
        }

    # --- 7. Lead: follow up? (stopping rules are code) ---
    def follow_up_node(state: ReviewState) -> dict[str, Any]:
        case = _case(state)
        outcomes = _outcomes(state)
        history = state.get("follow_up_history") or []
        reason = stop_reason(outcomes, state.get("internal_rounds", 0), state.get("last_round_new"),
                             has_internal_candidates(outcomes, history))
        if reason:
            return {"stop_reason": reason, "pending_follow_ups": []}
        facts = _facts(state, case)
        pending = next_follow_ups(case, outcomes, facts, history, state.get("internal_rounds", 0) + 1, deps.clock())
        for p in pending:
            p.ask = safe_ask(deps.llm.reword_ask(p.check_id, p.ask, p.reason), p.ask)
        if not pending:
            return {"stop_reason": "no_internal_follow_up_left", "pending_follow_ups": []}
        return {"stop_reason": None, "pending_follow_ups": [p.to_dict() for p in pending]}

    def fan_out_follow_ups(state: ReviewState):
        pending = [PendingFollowUp.from_dict(p) for p in state.get("pending_follow_ups") or []]
        if not pending:
            return "write_memo"
        case, plan = _case(state), _plan(state)
        results = [r for _, r in _results(state)]
        facts = _facts(state, case)
        round_no = state.get("internal_rounds", 0) + 1
        sends = []
        for slot, p in enumerate(pending):
            task = follow_up_task(case, plan, p, results, facts, state.get("initial_task_ids") or {}, slot)
            sends.append(Send("run_worker", {"task": _dump(task), "round": round_no}))
        return sends

    # --- 8. Lead: write memo ---
    def write_memo(state: ReviewState) -> dict[str, Any]:
        routing = _routing(state["routing"])
        case = _case(state)
        plan = _plan(state)
        outcomes = _outcomes(state)
        facts = _facts(state, case) if case else None
        attempts = state.get("memo_attempts", 0)
        revision = _revision(state.get("check_result"), state.get("lead_check_failures")) if attempts else None
        history = [FollowUpRecord.model_validate(h) for h in state.get("follow_up_history") or []]
        prose: Optional[MemoProse] = None
        if routing.route == "reviewed" and case is not None:
            prose = deps.llm.memo_prose({
                "case_summary": _summary(case, routing),
                "flags": [f.value for f in routing.open_flags],
                "checks": [_dump(o) for o in outcomes.values()],
                "follow_ups": [_dump(h) for h in history],
                "findings": finding_view(facts) if facts else [],
            }, revision.failures if revision else [])
        build = build_memo(
            case_id=routing.case_id or state["raw_case"].get("case_id"), case=case, routing=routing,
            plan=plan, outcomes=outcomes, follow_ups=history, facts=facts, prose=prose,
            now=deps.clock(), today=today(), version=attempts + 1, revision=revision,
            worker_errors=[f"{r.task_id}: {r.error}" for _, r in _results(state) if r.status == "failed"],
        )
        return {"memo": _dump(build.memo) if build.memo else None, "report": _dump(build.report),
                "memo_attempts": attempts + 1, "tin_redacted": build.tin_redacted}

    # --- 9. checks (code) ---
    def checks_node(state: ReviewState) -> dict[str, Any]:
        memo = Memo.model_validate(state["memo"]) if state.get("memo") else None
        report = LeadReport.model_validate(state["report"])
        case = _case(state)
        facts = _facts(state, case) if case else None
        checked = run_checks(memo, report, case, facts, _plan(state), tin_redacted=state.get("tin_redacted", False))
        out = ReviewOutput(
            checked_memo=(CheckedMemo(memo=memo, evidence_index=checked.evidence_index,
                                      check_result=checked.check_result) if memo else None),
            lead=report, lead_check_failures=checked.lead_failures,
            check_attempts=state.get("memo_attempts", 1), report_evidence_index=checked.report_index,
        )
        return {"check_result": _dump(checked.check_result) if checked.check_result else None,
                "lead_check_failures": [_dump(f) for f in checked.lead_failures],
                "evidence_index": {k: _dump(v) for k, v in checked.evidence_index.items()},
                "review_output": _dump(out)}

    def after_checks(state: ReviewState) -> str:
        out = ReviewOutput.model_validate(state["review_output"])
        if not out.passed and state.get("memo_attempts", 1) < 2:
            return "write_memo"       # sent back once
        return "human_review"

    # --- 10. analyst decision (interrupt) ---
    def human_review(state: ReviewState) -> dict[str, Any]:
        answer = interrupt({"review_id": state["review_id"], "review_output": state["review_output"]})
        return {"decision_record": dict(answer), "status": "deciding"}

    # --- 11. hand the result back to the backend API ---
    def finalize(state: ReviewState) -> dict[str, Any]:
        answer = state["decision_record"] or {}
        decision = DecisionInput.model_validate({"decision": answer.get("decision"), "note": answer.get("note", "")})
        report = LeadReport.model_validate(state["report"])
        recommendation = state["memo"]["recommendation"] if state.get("memo") else "insufficient_evidence"
        record = DecisionRecord(
            review_id=state["review_id"], case_id=report.case_id, decision=decision.decision, note=decision.note,
            analyst_id=str(answer.get("analyst_id") or "unknown"), agent_recommendation=recommendation,
            overridden=is_override(recommendation, decision.decision), decided_at=deps.clock(),
        )
        payload = {"decision_record": _dump(record), "case_status": STATUS_AFTER[decision.decision].value}
        if deps.result_sink:
            deps.result_sink(payload)
        return {"decision_record": _dump(record), "status": "done"}

    g = StateGraph(ReviewState)
    g.add_node("intake", intake)
    g.add_node("plan", plan_node)
    g.add_node("run_worker", run_worker)
    g.add_node("reconcile", reconcile_node)
    g.add_node("follow_up", follow_up_node)
    g.add_node("write_memo", write_memo)
    g.add_node("checks", checks_node)
    g.add_node("human_review", human_review)
    g.add_node("finalize", finalize)

    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", after_intake, ["plan", "write_memo"])
    g.add_conditional_edges("plan", fan_out_initial, ["run_worker", "reconcile"])
    g.add_edge("run_worker", "reconcile")
    g.add_edge("reconcile", "follow_up")
    g.add_conditional_edges("follow_up", fan_out_follow_ups, ["run_worker", "write_memo"])
    g.add_edge("write_memo", "checks")
    g.add_conditional_edges("checks", after_checks, ["write_memo", "human_review"])
    g.add_edge("human_review", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer if checkpointer is not None else MemorySaver())


def _revision(check_result: Optional[dict[str, Any]],
              lead_failures: Optional[list[dict[str, Any]]] = None) -> Revision:
    """What step 9's failures ask the memo writer to fix (memo.CheckRule and review.LeadRule)."""
    rev = Revision()
    failures = list(CheckResult.model_validate(check_result).failures) if check_result else []
    failures += [LeadCheckFailure.model_validate(f) for f in lead_failures or []]
    for f in failures:
        rev.failures.append(f"{f.rule}: {f.detail}")
        if f.rule in ("CLEAR_NEEDS_TWO_SOURCES", "NOT_ISSUED_TIN_CANT_CLEAR") and f.flag:
            rev.unresolve_flags.add(FlagReason(f.flag).value)
        elif f.rule in ("UNRELATED_EIN_MUST_ESCALATE", "HARD_FAIL_NOT_ESCALATED"):
            rev.force_escalate = True
        elif f.rule == "DOCUMENT_ALREADY_IN_CASE":
            rev.drop_doc_types.add(f.detail.rsplit(" ", 1)[-1])
    return rev


__all__ = ["Deps", "ResultSink", "ReviewState", "Worker", "build_graph"]
