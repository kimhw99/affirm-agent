"""ReviewService: runs reviews on the graph, keyed for idempotency, paused for the analyst.

- ``start``: the idempotency key is case_id + a hash of the input, so a retry of the same
  payload returns the same review (no second run), while a case that comes back after
  ``docs_requested`` with new evidence starts a new one. A run that crashed releases its
  key, so the backend's retry starts a fresh run instead of getting the failure back.
- ``status``: running | awaiting_decision | done | failed (the review status, not the case status).
- ``decide``: resumes the paused graph with the analyst's decision; the graph then hands
  the result back to the backend (``Deps.result_sink``). If that fails, for example
  because the case service refuses the decision (409), the review fails like a crashed
  run: ``status`` says ``failed`` with the reason, its key is released, and
  ``DecisionFailed`` carries the case service's status code, if it answered.

thread_id = review_id, so a paused review survives as long as the checkpointer does.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Optional

import httpx
from langgraph.types import Command

from app.graph.orchestrator import IdempotencyStore, idempotency_key
from kyb_contracts.decision import DecisionInput

log = logging.getLogger(__name__)


class ReviewNotFound(KeyError):
    pass


class NotAwaitingDecision(RuntimeError):
    pass


class DecisionFailed(RuntimeError):
    """The decision couldn't be finished, so the review failed. ``status_code`` is the case
    service's answer when it refused the decision, None for any other error."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _case_service_answer(exc: BaseException) -> Optional[httpx.Response]:
    """The case service's response, if an HTTP error from it raised ``exc`` or caused it."""
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.HTTPStatusError):
            return current.response
        current = current.__cause__ or current.__context__
    return None


def _detail(response: httpx.Response) -> str:
    """The reason the case service gave (FastAPI's ``detail``), or its status line."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    return str(detail or response.reason_phrase or response.status_code)


class ReviewService:
    def __init__(self, graph, store: Optional[IdempotencyStore] = None, callbacks: Optional[list] = None):
        self.graph = graph
        self.store = store or IdempotencyStore()
        self.callbacks = callbacks or []
        self._errors: dict[str, str] = {}
        self._known: set[str] = set()
        self._keys: dict[str, str] = {}      # review_id -> its idempotency key, released if it fails
        self._lock = threading.Lock()

    def _config(self, review_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": review_id}, "callbacks": self.callbacks,
                "run_name": f"review {review_id}"}

    # ----- start -----

    def start(self, raw_case: dict[str, Any], background: bool = True) -> tuple[str, bool]:
        key = idempotency_key(raw_case)
        review_id, created = self.store.get_or_claim(key, f"rv_{uuid.uuid4().hex[:8]}")
        if not created:
            return review_id, False
        with self._lock:
            self._known.add(review_id)
            self._keys[review_id] = key
        if background:
            threading.Thread(target=self._run, args=(review_id, raw_case, key), daemon=True).start()
        else:
            self._run(review_id, raw_case, key)
        return review_id, True

    def _run(self, review_id: str, raw_case: dict[str, Any], key: Optional[str] = None) -> None:
        try:
            self.graph.invoke({"review_id": review_id, "raw_case": raw_case}, self._config(review_id))
        except Exception as exc:  # noqa: BLE001
            log.exception("review %s failed", review_id)
            self._errors[review_id] = f"{type(exc).__name__}: {exc}"
            if key is not None:
                self.store.release(key)   # the failed review stays readable; a retry runs again

    # ----- status -----

    def status(self, review_id: str) -> dict[str, Any]:
        if review_id not in self._known:
            raise ReviewNotFound(review_id)
        if review_id in self._errors:
            return {"review_id": review_id, "status": "failed", "error": self._errors[review_id]}
        snap = self.graph.get_state(self._config(review_id))
        values = snap.values or {}
        out: dict[str, Any] = {"review_id": review_id, "case_id": (values.get("routing") or {}).get("case_id")}
        if values.get("status") == "done":
            out.update(status="done", review_output=values.get("review_output"),
                       decision_record=values.get("decision_record"))
        elif "human_review" in (snap.next or ()) and any(t.interrupts for t in snap.tasks or ()):
            out.update(status="awaiting_decision", review_output=values.get("review_output"))
        else:
            out["status"] = "running"
        return out

    # ----- decide -----

    def decide(self, review_id: str, decision: DecisionInput, analyst_id: str) -> dict[str, Any]:
        current = self.status(review_id)
        if current["status"] != "awaiting_decision":
            raise NotAwaitingDecision(f"review {review_id} is {current['status']}")
        resume = {"decision": decision.decision, "note": decision.note, "analyst_id": analyst_id}
        try:
            self.graph.invoke(Command(resume=resume), self._config(review_id))
        except Exception as exc:  # noqa: BLE001
            # Otherwise the graph stays stopped at step 11, so status() would say running for
            # good, and the key would hand that stuck review to every retry.
            log.exception("the decision on review %s failed", review_id)
            answer = _case_service_answer(exc)
            if answer is not None:
                message = f"the case service refused the decision ({answer.status_code}): {_detail(answer)}"
            else:
                message = f"{type(exc).__name__}: {exc}"
            self._errors[review_id] = message
            key = self._keys.get(review_id)
            if key is not None:
                self.store.release(key)   # as in _run: starting it again runs a new review
            raise DecisionFailed(message, answer.status_code if answer is not None else None) from exc
        return self.status(review_id)


__all__ = ["DecisionFailed", "NotAwaitingDecision", "ReviewNotFound", "ReviewService"]
