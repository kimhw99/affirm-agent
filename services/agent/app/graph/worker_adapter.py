"""Adapters between the Lead's ``Worker`` interface and the BEAC-7 workers.

The graph (``workflow.Deps``) calls ``worker.run(WorkerInput) -> WorkerResult``. The
BEAC-7 workers are functions, ``run_records_worker(task, client, llm=None, config=None,
now=None)`` and ``run_evidence_worker(...)`` in ``app.workers``, that need the case
service client for their tools. ``LangGraphWorker`` binds those arguments:

- ``client``: the ``CaseDataClient`` the tools read from (``HttpCaseClient`` in the
  service, ``FakeCaseClient`` over the golden-case folders in tests).
- ``llm``: the chat model; ``None`` lets the worker build its own (``app.llm``).
- ``callbacks``: the Lead's callbacks (LangFuse), passed through the worker's
  ``config`` so worker steps nest under the review's trace.
- ``clock``: the Lead's clock, so "today" is the same day on both sides (dates such as
  "formed N days ago" are computed by the tools).

The worker never raises; if it does anyway, the graph turns that into a failed result.
"""

from __future__ import annotations

import importlib
import logging
from datetime import datetime
from typing import Any, Callable, Optional

from kyb_contracts.worker_io import WorkerInput, WorkerResult

log = logging.getLogger(__name__)

WorkerFn = Callable[..., WorkerResult]

WORKER_FUNCTIONS = {
    "records": ("app.workers.records", "run_records_worker"),
    "evidence": ("app.workers.evidence", "run_evidence_worker"),
}


class LangGraphWorker:
    """A BEAC-7 worker function as a ``Worker`` (``run(WorkerInput) -> WorkerResult``)."""

    def __init__(self, worker: str, fn: WorkerFn, client: Any, llm: Any = None,
                 callbacks: Optional[list] = None, clock: Optional[Callable[[], datetime]] = None):
        self.worker = worker
        self.fn = fn
        self.client = client
        self.llm = llm
        self.callbacks = list(callbacks or [])
        self.clock = clock

    def llm_for(self, task: WorkerInput) -> Any:
        """The chat model for one task (``None``: the worker builds its own)."""
        return self.llm

    def run(self, task: WorkerInput) -> WorkerResult:
        if task.worker != self.worker:
            raise ValueError(f"the {self.worker} worker got a {task.worker} task")
        config: dict[str, Any] = {"tags": ["lead"], "metadata": {"follow_up_of": task.follow_up_of}}
        if self.callbacks:
            config["callbacks"] = self.callbacks
        return self.fn(task, self.client, llm=self.llm_for(task), config=config,
                       now=self.clock() if self.clock else None)


class PlaceholderWorker:
    """Stands in when a worker can't be loaded: every task fails, so every check that needs
    it ends as a GAP and the memo says insufficient_evidence instead of guessing."""

    def __init__(self, worker: str, reason: str):
        self.worker = worker
        self.reason = reason

    def run(self, task: WorkerInput) -> WorkerResult:
        return WorkerResult(task_id=task.task_id, case_id=task.context.case_id, worker=task.worker,
                            status="failed", findings=[], sources=[], hypothesis_signals=[],
                            tool_calls_used=0, error=f"the {self.worker} worker is unavailable: {self.reason}")


def load_worker_fn(worker: str) -> Optional[WorkerFn]:
    module, name = WORKER_FUNCTIONS[worker]
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as exc:
        log.warning("%s.%s not found (%s)", module, name, exc)
        return None


def build_workers(client: Any, llm: Any = None, callbacks: Optional[list] = None,
                  clock: Optional[Callable[[], datetime]] = None,
                  client_error: Optional[str] = None) -> tuple[Any, Any]:
    """(records, evidence) workers: the BEAC-7 workers when they and a client are there,
    otherwise placeholders that fail every task with the reason."""
    out = []
    for worker in ("records", "evidence"):
        fn = load_worker_fn(worker)
        if fn is None:
            out.append(PlaceholderWorker(worker, f"{'.'.join(WORKER_FUNCTIONS[worker])} is not installed"))
        elif client is None:
            out.append(PlaceholderWorker(worker, client_error or "no case service client"))
        else:
            out.append(LangGraphWorker(worker, fn, client, llm=llm, callbacks=callbacks, clock=clock))
    return out[0], out[1]


__all__ = ["LangGraphWorker", "PlaceholderWorker", "WORKER_FUNCTIONS", "build_workers", "load_worker_fn"]
