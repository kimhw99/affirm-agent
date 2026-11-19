"""The chat model the workers use (OpenAI, through langchain-openai).

Callbacks (for example the lead's LangFuse handler) are not set here: they arrive
through the ``config`` passed to each ``invoke``, so worker steps nest under the
lead's trace.
"""

from __future__ import annotations

import os
from typing import Any


def get_chat_model(**overrides: Any):
    """``ChatOpenAI`` for ``OPENAI_MODEL``, deterministic, with a timeout and two retries.

    Imported lazily so tests (which use a scripted fake model) don't need an API key.
    """
    from langchain_openai import ChatOpenAI

    model = os.environ.get("OPENAI_MODEL")
    if not model:
        raise RuntimeError("OPENAI_MODEL is not set; see .env.example")
    settings: dict[str, Any] = {"model": model, "temperature": 0, "timeout": 60, "max_retries": 2}
    settings.update(overrides)
    return ChatOpenAI(**settings)
