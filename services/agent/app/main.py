"""FastAPI entry point.

    uvicorn app.main:app --reload        (from services/agent)

Workers: the BEAC-7 Records and Evidence workers (``app.workers.run_records_worker`` /
``run_evidence_worker``), reading the case service through ``HttpCaseClient``
(``CASE_SERVICE_URL``, ``CASE_SERVICE_TOKEN``) and using the chat model from
``app.llm`` (``OPENAI_MODEL``). If either is missing, a placeholder worker fails each
task with the reason, so reviews still run end to end and every check that needs a
worker ends as a GAP (insufficient_evidence) instead of guessing.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI

from app.api.reviews import router
from app.api.service import ReviewService
from app.clients.case_service import CaseServiceClient
from app.graph.llm import default_llm
from app.graph.worker_adapter import build_workers
from app.graph.workflow import Deps, build_graph
from app.observability.tracing import langfuse_callbacks

log = logging.getLogger(__name__)


def _worker_client() -> tuple[Any, Optional[str]]:
    """The case-service client the workers' tools read from, or (None, why not)."""
    try:
        from app.clients.case_client import HttpCaseClient
    except ImportError as exc:
        return None, f"app.clients.case_client is not installed ({exc})"
    try:
        return HttpCaseClient(), None
    except Exception as exc:  # noqa: BLE001 - e.g. CASE_SERVICE_URL is not set
        log.warning("The workers' case-service client could not start: %s", exc)
        return None, str(exc)


def create_app(deps: Optional[Deps] = None, case_client: Any = None, background: bool = True) -> FastAPI:
    callbacks = langfuse_callbacks()
    case_client = case_client or CaseServiceClient()
    if deps is None:
        clock = lambda: datetime.now(timezone.utc)  # noqa: E731
        worker_client, why = _worker_client()
        records, evidence = build_workers(worker_client, callbacks=callbacks, clock=clock, client_error=why)
        deps = Deps(
            records_worker=records,
            evidence_worker=evidence,
            llm=default_llm(callbacks),
            clock=clock,
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
