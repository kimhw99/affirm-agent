"""Evidence worker: the merchant's documents, bank verification and website snapshots.
Cites D, B and W sources only."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from kyb_contracts.worker_io import WorkerInput, WorkerResult

from app.clients.case_client import CaseDataClient

from .base import run_worker


def run_evidence_worker(task: WorkerInput, client: CaseDataClient, llm: Any = None,
                        config: Optional[dict] = None, now: Optional[datetime] = None) -> WorkerResult:
    """Run an Evidence task. ``llm`` defaults to ``get_chat_model()``; never raises."""
    return run_worker(task, client, llm=llm, config=config, now=now)
