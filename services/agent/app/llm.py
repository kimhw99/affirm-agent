"""The chat model the workers use (OpenAI, through langchain-openai).

The model name and the API key are read here for both the workers and the lead
(``app.graph.llm``), so the two can't disagree: with ``OPENAI_MODEL`` unset, both use
``DEFAULT_MODEL``, the same default as docker-compose.yml and .env.example.

Callbacks (for example the lead's LangFuse handler) are not set here: they arrive
through the ``config`` passed to each ``invoke``, so worker steps nest under the
lead's trace.
"""

from __future__ import annotations

import os
from typing import Any, Optional

DEFAULT_MODEL = "gpt-4o-mini"


def default_model() -> str:
    """``OPENAI_MODEL``, or ``DEFAULT_MODEL`` when it is unset or empty."""
    return os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL


def openai_api_key() -> Optional[str]:
    """``OPENAI_API_KEY`` (``OPAI_API_KEY`` is still accepted, as the lead always has)."""
    return os.environ.get("OPENAI_API_KEY") or os.environ.get("OPAI_API_KEY") or None


def get_chat_model(**overrides: Any):
    """``ChatOpenAI`` for ``default_model()``, deterministic, with a timeout and two retries.

    Raises ``RuntimeError`` when no API key is set, so the worker fails with a clear
    reason instead of an error from deep inside the OpenAI client. Imported lazily so
    tests (which use a scripted fake model) don't need an API key.
    """
    from langchain_openai import ChatOpenAI

    settings: dict[str, Any] = {"model": default_model(), "temperature": 0, "timeout": 60, "max_retries": 2}
    api_key = openai_api_key()
    if api_key:
        settings["api_key"] = api_key
    settings.update(overrides)
    if not settings.get("api_key"):
        raise RuntimeError("OPENAI_API_KEY is not set; see .env.example")
    return ChatOpenAI(**settings)
