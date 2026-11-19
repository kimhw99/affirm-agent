"""Records worker: official records (registry, filing history, DBA, IRS TIN matching) and
third-party data (commercial database, owner KYC). Cites R and K sources only."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from kyb_contracts.worker_io import WorkerInput, WorkerResult

from app.clients.case_client import CaseDataClient

from .base import run_worker


def run_records_worker(task: WorkerInput, client: CaseDataClient, llm: Any = None,
                       config: Optional[dict] = None, now: Optional[datetime] = None) -> WorkerResult:
    """Run a Records task. ``llm`` defaults to ``get_chat_model()``; never raises."""
    return run_worker(task, client, llm=llm, config=config, now=now)
