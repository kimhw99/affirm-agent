"""LangFuse callbacks. Empty unless LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set."""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)


def langfuse_callbacks() -> list[Any]:
    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        return []
    try:  # langfuse v3
        from langfuse.langchain import CallbackHandler  # type: ignore
    except ImportError:
        try:  # langfuse v2
            from langfuse.callback import CallbackHandler  # type: ignore
        except ImportError:
            log.warning("langfuse is not installed; tracing is off")
            return []
    try:
        return [CallbackHandler()]
    except Exception as exc:  # noqa: BLE001
        log.warning("LangFuse callback could not start (%s); tracing is off", exc)
        return []


__all__ = ["langfuse_callbacks"]
