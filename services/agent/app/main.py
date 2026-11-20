"""FastAPI entry point.

    uvicorn app.main:app --reload        (from services/agent)

Workers are loaded from ``app.workers.records.RecordsWorker`` and
``app.workers.evidence.EvidenceWorker`` (BEAC-4). Until those exist, a placeholder
worker returns a failed result, so reviews still run end to end and every check that
needs a worker ends as a GAP (insufficient_evidence) instead of guessing.
"""

from __future__ import annotations

import importlib
import logging
import os
from typing import Any, Optional

from fastapi import FastAPI

from app.api.reviews import router
from app.api.service import ReviewService
from app.clients.case_service import CaseServiceClient
from app.graph.llm import default_llm
from app.graph.workflow import Deps, build_graph
from app.observability.tracing import langfuse_callbacks
from kyb_contracts.worker_io import WorkerInput, WorkerResult

log = logging.getLogger(__name__)


class PlaceholderWorker:
    def __init__(self, worker: str):
        self.worker = worker

    def run(self, task: WorkerInput) -> WorkerResult:
        return WorkerResult(task_id=task.task_id, case_id=task.context.case_id, worker=task.worker,
                            status="failed", findings=[], sources=[], hypothesis_signals=[],
                            tool_calls_used=0, error=f"the {self.worker} worker is not installed yet")


def _load_worker(module: str, cls: str, name: str):
    try:
        return getattr(importlib.import_module(module), cls)()
    except (ImportError, AttributeError):
        log.warning("%s.%s not found; using a placeholder %s worker", module, cls, name)
        return PlaceholderWorker(name)


def create_app(deps: Optional[Deps] = None, case_client: Any = None, background: bool = True) -> FastAPI:
    callbacks = langfuse_callbacks()
    case_client = case_client or CaseServiceClient()
    if deps is None:
        deps = Deps(
            records_worker=_load_worker("app.workers.records", "RecordsWorker", "records"),
            evidence_worker=_load_worker("app.workers.evidence", "EvidenceWorker", "evidence"),
            llm=default_llm(callbacks),
            result_sink=case_client.send_decision if os.getenv("SEND_RESULTS", "1") != "0" else None,
            callbacks=callbacks,
        )
    app = FastAPI(title="KYB agent service")
    app.state.reviews = ReviewService(build_graph(deps), callbacks=callbacks)
    app.state.case_client = case_client
    app.state.background = background
    app.include_router(router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
