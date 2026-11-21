"""Where a worker's tool path goes: to a recorder, and only when one is set up.

The tool path is for the evaluation, never for the Lead (contracts/kyb_contracts/tool_path.py):
``WorkerResult`` doesn't carry it, and the Lead's code never calls ``record_tool_paths``.
An evaluation or a test wraps a review, or a single worker run, in it:

    with record_tool_paths() as paths:
        graph.invoke(...)            # or run_records_worker(...)
    # paths: one ToolPath per worker task, first tasks and follow-ups

The recorder is a context variable, so it reaches workers that the Lead's graph runs in
parallel threads (LangGraph copies the context into them) without the Lead passing it on.
Outside a recorder, ``publish`` drops the path.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from kyb_contracts.tool_path import ToolPath

_RECORDER: ContextVar[Optional[list[ToolPath]]] = ContextVar("tool_path_recorder", default=None)
_LOCK = threading.Lock()   # parallel workers append to the same list


@contextmanager
def record_tool_paths() -> Iterator[list[ToolPath]]:
    paths: list[ToolPath] = []
    token = _RECORDER.set(paths)
    try:
        yield paths
    finally:
        _RECORDER.reset(token)


def publish(path: ToolPath) -> None:
    paths = _RECORDER.get()
    if paths is not None:
        with _LOCK:
            paths.append(path)


__all__ = ["publish", "record_tool_paths"]
