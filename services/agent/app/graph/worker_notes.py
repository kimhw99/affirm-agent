"""What the workers say about their own results, beyond the findings.

A ``WorkerResult`` carries, besides findings and sources:

- ``status`` / ``error``: ``failed`` (no usable result) or ``partial`` (a lookup or the
  summary failed, or the tool budget ran out),
- ``not_found``: things the worker looked for and found no record of,
- ``unresolved``: questions it couldn't settle (budget, conflicting records),
- ``answer`` and ``hypothesis_signals``: its own reading of its findings.

The Lead uses them as follows. Verdicts still come only from the findings (reconcile.py);
these never change a check's status.

- ``unresolved`` / ``not_found`` -> follow-ups: a check whose worker left something
  unresolved gets its next follow-up from that worker first, and every follow-up tells
  the worker what is still open and what was already searched for in vain
  (followup.py). In the memo they explain why a check is still a GAP or a CONFLICT.
- ``status`` / ``error`` -> the memo: ``failed`` tasks are worker errors (the merchant
  isn't asked for documents to cover our failure); ``partial`` tasks are listed as
  warnings.
- ``answer`` / ``hypothesis_signals`` -> the memo-prose LLM, as input for root causes
  and ruled-out causes. They come from the same summary call as ``unresolved`` and
  ``not_found``, so keeping them costs nothing extra.

A note is tied to a check when it came from a follow-up for that check, names the check
ID, or names one of the check's focus fields on that worker's side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal, Optional, Sequence

from app.graph.plan import Plan, PlannedCheck
from kyb_contracts.worker_io import WorkerResult

NoteKind = Literal["unresolved", "not_found"]

MAX_NOTE_CHARS = 160
MAX_NOTES_PER_CHECK = 3

# What the worker loop writes on its own (app/workers/base.py); not about any check.
_BOILERPLATE = ("No summary was produced", "A recommendation to approve or deny was removed")
# Focus fields too generic to tie free text to one check.
_GENERIC_FIELDS = frozenset({"name", "date", "status", "holder"})


@dataclass(frozen=True)
class WorkerNote:
    task_id: str
    worker: str              # "records" | "evidence"
    kind: NoteKind
    text: str

    def render(self) -> str:
        label = "still unresolved" if self.kind == "unresolved" else "not found"
        return f"{self.worker} worker, {label}: {self.text}"


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_NOTE_CHARS else text[:MAX_NOTE_CHARS - 1].rstrip() + "…"


def notes_of(result: WorkerResult) -> list[WorkerNote]:
    out = []
    for kind, items in (("unresolved", result.unresolved), ("not_found", result.not_found)):
        for item in items:
            if item and item.strip() and not item.startswith(_BOILERPLATE):
                out.append(WorkerNote(result.task_id, result.worker, kind, _short(item)))
    return out


# ---------- Tying notes to checks ----------

def _mentions(text: str, check: PlannedCheck, worker: str) -> bool:
    low = text.lower()
    if check.check_id.lower() in low:
        return True
    focus = check.records_focus if worker == "records" else check.evidence_focus
    for f in focus:
        if f in _GENERIC_FIELDS:
            continue
        if f.lower() in low or f.replace("_", " ").lower() in low:
            return True
    return False


def task_checks(history: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Follow-up task_id -> the check it was sent for (from the follow-up history)."""
    return {h["task_id"]: h["check_id"] for h in history if h.get("task_id")}


def notes_by_check(plan: Optional[Plan], results: Sequence[WorkerResult],
                   history: Iterable[dict[str, Any]] = ()) -> tuple[dict[str, list[WorkerNote]], list[WorkerNote]]:
    """(notes per check, notes tied to no check).

    Per check and worker, only the newest relevant task counts: the latest follow-up for
    that check if there was one (it supersedes what the first pass left open), else the
    worker's first task.
    """
    if plan is None:
        return {}, [n for r in results for n in notes_of(r)]
    sent_for = task_checks(history)
    per_check: dict[str, list[WorkerNote]] = {}
    used: set[WorkerNote] = set()
    first = {r.worker: r for r in results if r.task_id not in sent_for}   # newest non-follow-up task
    for check in plan.checks:
        notes: list[WorkerNote] = []
        for worker in ("records", "evidence"):
            follow = [r for r in results if r.worker == worker and sent_for.get(r.task_id) == check.check_id]
            if follow:
                notes += notes_of(follow[-1])      # sent for this check: every note is about it
                continue
            base = first.get(worker)
            if base is not None:
                notes += [n for n in notes_of(base) if _mentions(n.text, check, worker)]
        if notes:
            per_check[check.check_id] = notes[:MAX_NOTES_PER_CHECK]
            used.update(notes)
    # A note from a follow-up names its check in the task question ("[EC2.tin_match] ...")
    # and was handled above; anything left is general.
    loose = [n for r in results for n in notes_of(r) if n not in used and r.task_id not in sent_for]
    return per_check, list(dict.fromkeys(loose))


def unresolved_workers(notes: Iterable[WorkerNote]) -> set[str]:
    return {n.worker for n in notes if n.kind == "unresolved"}


def follow_up_context(notes: Sequence[WorkerNote], target: str) -> str:
    """One sentence for a follow-up question: what the target worker left open last time."""
    mine = [n for n in notes if n.worker == target]
    parts = []
    open_ = [n.text for n in mine if n.kind == "unresolved"]
    missing = [n.text for n in mine if n.kind == "not_found"]
    if open_:
        parts.append("still unresolved from your last pass: " + "; ".join(open_))
    if missing:
        parts.append("already searched with no record found: " + "; ".join(missing)
                     + " (try a different search rather than repeating it)")
    return ("Context: " + ". ".join(parts) + ".") if parts else ""


# ---------- Worker status ----------

def worker_issues(results: Sequence[WorkerResult]) -> tuple[list[str], list[str]]:
    """(errors, warnings): failed tasks, and partial ones with what went wrong."""
    errors, warnings = [], []
    for r in results:
        if r.status == "failed":
            errors.append(f"{r.task_id}: {r.error or 'failed'}")
        elif r.status == "partial":
            why = r.error or ("tool budget used up" if r.unresolved else "incomplete")
            warnings.append(f"{r.task_id}: {why}")
    return errors, warnings


# ---------- For the memo-prose LLM ----------

def worker_reports(results: Sequence[WorkerResult], plan: Optional[Plan]) -> list[dict[str, Any]]:
    """Each task's own summary: its answer, signals (with the hypothesis text and the
    evidence IDs behind them), what it couldn't find or settle, and its status."""
    hyp = {h.hypothesis_id: h.text for h in (plan.hypotheses if plan else [])}
    out = []
    for r in results:
        src = {f.finding_id: f.source_id for f in r.findings}
        out.append({
            "task_id": r.task_id,
            "worker": r.worker,
            "status": r.status,
            "error": r.error,
            "answer": r.answer,
            "hypothesis_signals": [
                {"hypothesis": hyp.get(s.hypothesis_id, s.hypothesis_id), "direction": s.direction,
                 "finding_keys": [f"{r.task_id}:{fid}" for fid in s.finding_ids],
                 "evidence": list(dict.fromkeys(src[fid] for fid in s.finding_ids if fid in src))}
                for s in r.hypothesis_signals
            ],
            "not_found": list(r.not_found),
            "unresolved": list(r.unresolved),
        })
    return out


__all__ = [
    "WorkerNote",
    "follow_up_context",
    "notes_by_check",
    "notes_of",
    "task_checks",
    "unresolved_workers",
    "worker_issues",
    "worker_reports",
]
