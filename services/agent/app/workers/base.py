"""The worker loop shared by the Records and Evidence workers (LangGraph).

    agent (chat model + bound tools) --tool calls?--> tools (permission + key + budget guard) --> agent
          '-- no calls, but a new step is owed -----> remind (each step once; checklist.py) -----> agent
          '-- no calls / budget used up ------------> summarize (structured WorkerSummary)
          '-- LLM error ----------------------------> end
    then assemble_result() turns the findings and the summary into one WorkerResult.

The model picks tools and interprets results; code writes every finding. ``run_worker``
never raises: a tool or case-service error gives ``partial``, an LLM or graph failure
gives ``failed``, and either way the lead gets a valid ``WorkerResult``.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Annotated, Any, Literal, Optional, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kyb_contracts.tool_path import ToolCall, ToolPath
from kyb_contracts.worker_io import HypothesisSignal, WorkerInput, WorkerResult

from app.clients.case_client import CaseDataClient
from app.tools import TOOLS_FOR
from app.tools._common import ToolContext, ToolSpec, redact

from .checklist import missing_steps, reminder
from .tool_path import publish
from .prompts import SUMMARY_INSTRUCTIONS, render_findings, render_task, system_prompt

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Structured summary (valid for OpenAI strict mode: every field required, no extras)
# --------------------------------------------------------------------------

class SignalOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str
    direction: Literal["supports", "contradicts", "neutral"]
    finding_ids: list[str]


class WorkerSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(description="2-4 sentences answering the question, citing finding ids.")
    hypothesis_signals: list[SignalOut]
    not_found: list[str]
    unresolved: list[str]


# --------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------

class WorkerState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    tool_calls_used: int
    budget_hit: bool
    reminded: list[str]           # checklist steps already sent to the model (each at most once)
    llm_error: Optional[str]
    summary: Optional[WorkerSummary]
    summary_error: Optional[str]


def _redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """The model's arguments as recorded in the tool path: strings redacted, like everything shared."""
    return {k: redact(v) if isinstance(v, str) else v for k, v in args.items()}


def _error_text(error: BaseException) -> str:
    return redact(f"{type(error).__name__}: {error}")[:500]


def build_worker_graph(worker: str, tools: dict[str, ToolSpec], llm: Any, ctx: ToolContext):
    """Compile the agent -> tools -> ... -> summarize graph for one task."""
    task = ctx.task
    budget = task.max_tool_calls
    bound = llm.bind_tools([spec.openai_schema() for spec in tools.values()])

    def agent(state: WorkerState, config) -> dict:
        if state.get("tool_calls_used", 0) >= budget:
            return {"budget_hit": True}
        try:
            reply = bound.invoke(state["messages"], config=config)
        except Exception as error:  # the model or the network failed
            log.warning("%s worker LLM call failed: %s", worker, error)
            return {"llm_error": f"LLM call failed: {_error_text(error)}"}
        return {"messages": [reply]}

    def after_agent(state: WorkerState) -> str:
        if state.get("llm_error"):
            return END
        if state.get("budget_hit"):
            return "summarize"
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        if state.get("tool_calls_used", 0) < budget and _new_steps(state):
            return "remind"
        return "summarize"

    def _new_steps(state: WorkerState) -> list[str]:
        sent = set(state.get("reminded") or [])
        return [s for s in missing_steps(ctx) if s not in sent]

    def remind(state: WorkerState) -> dict:
        """The model stopped while the results still call for a lookup it hasn't been told about:
        say which. A step is never sent twice, so the model can still decline."""
        steps = _new_steps(state)
        return {"messages": [HumanMessage(content=reminder(steps))],
                "reminded": [*(state.get("reminded") or []), *steps]}

    def run_tools(state: WorkerState) -> dict:
        used = state.get("tool_calls_used", 0)
        budget_hit = state.get("budget_hit", False)
        turn = sum(1 for m in state["messages"] if isinstance(m, AIMessage) and m.tool_calls)
        replies = []
        for call in state["messages"][-1].tool_calls:
            args = call.get("args") or {}
            sources: list[str] = []
            if used >= budget:
                budget_hit = True
                status = "over_budget"
                content = json.dumps({"tool": call["name"], "rejected": "not run: the tool-call budget is used up"})
            else:
                used += 1  # every requested call counts, including rejected ones
                spec = tools.get(call["name"])
                if spec is None:
                    status = "rejected"
                    content = json.dumps({"tool": call["name"],
                                          "rejected": f"{call['name']} is not available to the {worker} worker; "
                                                      f"available: {sorted(tools)}"})
                else:
                    output = spec.run(ctx, args)
                    sources = [s.source_id for s in output.sources]
                    status = ("rejected" if output.rejected else "error" if output.error
                              else "repeat" if output.cached else "ok")
                    content = output.llm_view()
                content += f"\n[budget: {budget - used} tool call(s) left]"
            ctx.tool_path.append(ToolCall(turn=turn, tool=call["name"], args=_redact_args(args),
                                          source_ids=sources, status=status))
            replies.append(ToolMessage(content=content, tool_call_id=call["id"], name=call["name"]))
        ctx.tool_calls_used = used
        return {"messages": replies, "tool_calls_used": used, "budget_hit": budget_hit or used >= budget}

    def after_tools(state: WorkerState) -> str:
        return "summarize" if state.get("tool_calls_used", 0) >= budget else "agent"

    def summarize(state: WorkerState, config) -> dict:
        note = ""
        if state.get("budget_hit"):
            note = "\nThe tool-call budget ran out; list what you could not check under unresolved.\n"
        prompt = (render_task(task) + "\n\nFindings (the only ones you may cite):\n"
                  + render_findings(ctx.findings) + "\n\n" + SUMMARY_INSTRUCTIONS + note)
        try:
            structured = llm.with_structured_output(WorkerSummary, method="json_schema", strict=True)
            out = structured.invoke([SystemMessage(content=system_prompt(worker)), HumanMessage(content=prompt)],
                                    config=config)
            summary = out if isinstance(out, WorkerSummary) else WorkerSummary.model_validate(out)
        except Exception as error:
            log.warning("%s worker summary failed: %s", worker, error)
            return {"summary_error": f"summary failed: {_error_text(error)}"}
        return {"summary": summary}

    graph = StateGraph(WorkerState)
    graph.add_node("agent", agent)
    graph.add_node("tools", run_tools)
    graph.add_node("summarize", summarize)
    graph.add_node("remind", remind)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", after_agent, ["tools", "remind", "summarize", END])
    graph.add_edge("remind", "agent")
    graph.add_conditional_edges("tools", after_tools, ["agent", "summarize"])
    graph.add_edge("summarize", END)
    return graph.compile()


# --------------------------------------------------------------------------
# Result assembly
# --------------------------------------------------------------------------

_RECOMMENDATION = re.compile(r"\b(approv\w*|den(?:y|ies|ied|ial)|declin\w*|reject\w*)\b", re.IGNORECASE)
_SENTENCES = re.compile(r"(?<=[.!?])\s+")
RECOMMENDATION_REMOVED = ("A recommendation to approve or deny was removed from the answer; "
                          "workers report findings only.")


_FIELD_TOKEN = re.compile(r"([A-Za-z_]+)\s*(?:$|[:(\-])")


def found_fields(ctx: ToolContext) -> set[str]:
    """Finding fields with a non-empty value: the summary may not report these as not found."""
    return {f.field.casefold() for f in ctx.findings if f.value not in (None, "", [], {})}


def _leading_field(entry: str) -> str:
    """The field a not_found entry is about, when the entry is a field name: "former_name" or
    "former_name: no filing" -> former_name; free text ("dba certificate in county records") -> ""."""
    m = _FIELD_TOKEN.match(entry.strip())
    return m.group(1).casefold() if m else ""


def strip_recommendation(answer: Optional[str]) -> tuple[Optional[str], bool]:
    """Drop sentences that recommend approving or denying. Returns (answer, removed?)."""
    if not answer:
        return answer, False
    sentences = _SENTENCES.split(answer.strip())
    kept = [s for s in sentences if not _RECOMMENDATION.search(s)]
    if len(kept) == len(sentences):
        return answer.strip(), False
    return (" ".join(kept).strip() or None), True


def assemble_result(task: WorkerInput, ctx: ToolContext, state: Optional[WorkerState] = None,
                    error: Optional[str] = None) -> WorkerResult:
    state = state or {}
    summary: Optional[WorkerSummary] = state.get("summary")
    finding_ids = {f.finding_id for f in ctx.findings}
    hypothesis_ids = {h.hypothesis_id for h in task.hypotheses}

    signals: list[HypothesisSignal] = []
    answer, not_found, unresolved = None, [], []
    if summary is not None:
        for sig in summary.hypothesis_signals:
            cited = [fid for fid in dict.fromkeys(sig.finding_ids) if fid in finding_ids]
            if sig.hypothesis_id in hypothesis_ids and cited:
                signals.append(HypothesisSignal(hypothesis_id=sig.hypothesis_id, direction=sig.direction,
                                                finding_ids=cited))
        answer, removed = strip_recommendation(summary.answer)
        found = found_fields(ctx)
        not_found = [redact(s) for s in summary.not_found if _leading_field(s) not in found]
        unresolved = [redact(s) for s in summary.unresolved]
        if removed:
            unresolved.append(RECOMMENDATION_REMOVED)
        answer = redact(answer) if answer else None
    elif state.get("summary_error") or error or state.get("llm_error"):
        unresolved.append("No summary was produced; the findings are listed without interpretation.")

    fatal = [e for e in (error, state.get("llm_error")) if e]
    soft = list(ctx.errors) + ([state["summary_error"]] if state.get("summary_error") else [])
    if fatal:
        status = "failed"
    elif soft or summary is None or (state.get("budget_hit") and unresolved):
        status = "partial"
    else:
        status = "completed"
    message = "; ".join(fatal + soft) or None

    fields = dict(task_id=task.task_id, case_id=task.context.case_id, worker=task.worker, status=status,
                  findings=list(ctx.findings), sources=list(ctx.sources.values()), hypothesis_signals=signals,
                  not_found=not_found, unresolved=unresolved, answer=answer,
                  tool_calls_used=max(state.get("tool_calls_used", 0), ctx.tool_calls_used), error=message)
    try:
        return WorkerResult(**fields)
    except ValidationError as invalid:  # should not happen; still hand the lead something valid
        log.error("worker result failed validation: %s", invalid)
        return WorkerResult(task_id=task.task_id, case_id=task.context.case_id, worker=task.worker,
                            status="failed", findings=[], sources=[], hypothesis_signals=[],
                            unresolved=["The worker's result failed validation; no findings are reported."],
                            tool_calls_used=ctx.tool_calls_used,
                            error=f"invalid result: {_error_text(invalid)}")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def run_worker(task: WorkerInput, client: CaseDataClient, llm: Any = None, config: Optional[dict] = None,
               now: Optional[datetime] = None) -> WorkerResult:
    """Run one worker task to a ``WorkerResult``. Never raises."""
    worker = task.worker
    ctx = ToolContext(client, task, now=now)
    state: Optional[WorkerState] = None
    try:
        if llm is None:
            from app.llm import get_chat_model
            llm = get_chat_model()
        graph = build_worker_graph(worker, TOOLS_FOR[worker], llm, ctx)
        config = dict(config or {})
        run_config = {
            **config,
            # agent+tools per tool turn, plus agent+remind per reminder: a reminder needs a new
            # step, which only a tool turn (or the start) can bring, so at most budget + 1 of them
            "recursion_limit": 4 * task.max_tool_calls + 10,
            "run_name": f"{worker}_worker",
            "tags": [*config.get("tags", []), f"worker:{worker}"],
            "metadata": {**config.get("metadata", {}), "case_id": task.context.case_id,
                         "task_id": task.task_id, "worker": worker},
        }
        initial: WorkerState = {
            "messages": [SystemMessage(content=system_prompt(worker)), HumanMessage(content=render_task(task))],
            "tool_calls_used": 0, "budget_hit": False,
        }
        state = graph.invoke(initial, config=run_config)
        result = assemble_result(task, ctx, state)
    except Exception as error:  # graph, recursion limit, model setup
        log.exception("%s worker failed on %s", worker, task.task_id)
        result = assemble_result(task, ctx, state, error=f"worker failed: {_error_text(error)}")
    publish_tool_path(task, ctx, result)
    return result


def publish_tool_path(task: WorkerInput, ctx: ToolContext, result: WorkerResult) -> None:
    """Hand the task's tool path to the evaluation's recorder, if one is set up (tool_path.py).
    It never goes into the WorkerResult: the Lead doesn't see which tools ran."""
    try:
        listed = {s.source_id for s in result.sources}
        calls = [c if set(c.source_ids) <= listed else   # a result that failed validation lists no sources
                 c.model_copy(update={"source_ids": [s for s in c.source_ids if s in listed]})
                 for c in ctx.tool_path]
        publish(ToolPath(task_id=task.task_id, case_id=task.context.case_id, worker=task.worker, tool_calls=calls))
    except Exception:  # noqa: BLE001 - the evaluation's record must never break a review
        log.exception("could not publish the tool path of %s", task.task_id)
