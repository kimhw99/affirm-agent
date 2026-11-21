"""FastAPI entry point.

    uvicorn app.main:app --reload        (from services/agent)

Workers: the BEAC-7 Records and Evidence workers (``app.workers.run_records_worker`` /
``run_evidence_worker``), reading the case service through ``HttpCaseClient``
(``CASE_SERVICE_URL``, default ``http://case:8000``; ``CASE_SERVICE_TOKEN``) and using
the chat model from ``app.llm`` (``OPENAI_MODEL``, default ``gpt-4o-mini``, the same as
the Lead). If a worker or its client can't be loaded, a placeholder worker fails each
task with the reason; without ``OPENAI_API_KEY`` every worker task fails. Reviews still
run end to end and every check that needs a worker ends as a GAP
(insufficient_evidence) instead of guessing. Both cases are logged as warnings at
startup, so a review full of GAPs is never a surprise.

Logging (app/api/logs.py): every line goes to stderr with the time (UTC), the level,
the service, the request's X-Request-ID and the message, at LOG_LEVEL (INFO when unset).
The ID comes from the gateway; the case-service client passes it on, so the case
service's lines for a review's case and decision carry it too.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI

from app.api import logs
from app.api.reviews import router
from app.api.service import ReviewService
from app.clients.case_service import CaseServiceClient
from app.graph.llm import default_llm
from app.graph.worker_adapter import PlaceholderWorker, build_workers
from app.graph.workflow import Deps, build_graph
from app.llm import openai_api_key
from app.observability.tracing import langfuse_callbacks

logs.configure()
log = logging.getLogger(__name__)


class RequestIdCaseServiceClient(CaseServiceClient):
    """The case-service client, passing on the X-Request-ID of the request being handled."""

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        if (current := logs.request_id.get()) != logs.NO_REQUEST:
            headers[logs.HEADER] = current
        return headers


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


def _warn_if_workers_cannot_run(*workers: Any) -> None:
    """Say at startup when the workers will fail every task, instead of only in each review."""
    for worker in workers:
        if isinstance(worker, PlaceholderWorker):
            log.warning("The %s worker is a placeholder and will fail every task: %s",
                        worker.worker, worker.reason)
    if not openai_api_key():
        log.warning("OPENAI_API_KEY is not set: the Lead runs on its rules only and every worker task fails")


def create_app(deps: Optional[Deps] = None, case_client: Any = None, background: bool = True) -> FastAPI:
    callbacks = langfuse_callbacks()
    case_client = case_client or RequestIdCaseServiceClient()
    if deps is None:
        clock = lambda: datetime.now(timezone.utc)  # noqa: E731
        worker_client, why = _worker_client()
        records, evidence = build_workers(worker_client, callbacks=callbacks, clock=clock, client_error=why)
        _warn_if_workers_cannot_run(records, evidence)
        deps = Deps(
            records_worker=records,
            evidence_worker=evidence,
            llm=default_llm(callbacks),
            clock=clock,
            result_sink=case_client.send_decision if os.getenv("SEND_RESULTS", "1") != "0" else None,
            callbacks=callbacks,
        )
    app = FastAPI(title="KYB agent service")
    logs.track_requests(app)
    app.state.reviews = ReviewService(build_graph(deps), callbacks=callbacks)
    app.state.case_client = case_client
    app.state.background = background
    app.include_router(router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
