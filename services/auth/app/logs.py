"""Application logging for the auth service. Each service sets up its own, the same way.

One line per event, on stderr:

    2026-11-20T21:04:05.123Z INFO gateway request_id=0c6f2a1e-... app.main: method=GET path=/cases ...

that is, the time in UTC, the level, the service, the request's ID and the message.
LOG_LEVEL (INFO when unset) sets the level. uvicorn's own lines (startup, its access
line for each request, an unhandled error) come out the same way, except its access
lines for /health and /healthz, which Docker asks for every few seconds.

The request ID is the X-Request-ID header. The gateway gives every request one (or keeps
the client's, if it's valid), passes it to the service it forwards to, and returns it
in the response; each service puts it on every line it logs while handling the request,
uvicorn's included.

No line may hold a password, a JWT, a token, an API key or a full SSN or EIN, and the
code never logs one. As a last guard, the formatter blanks out of each message this
service's own secrets (SECRETS, read from the environment) and anything shaped like a
JWT, a Bearer token, an API key or a full SSN or EIN, whoever logged it.
"""

import logging
import os
import re
import time
from contextvars import ContextVar

from fastapi import FastAPI, Request

from kyb_contracts.case_input import TIN_PATTERN

# ---------- What differs between the services ----------
SERVICE = "auth"
SECRETS = ("AUTH_DB_PASSWORD", "JWT_SECRET", "DEMO_ANALYST_PASSWORD", "DEMO_SENIOR_PASSWORD")
QUIET = ("sqlalchemy",)                              # at INFO it would log SQL with its parameters
# -------------------------------------------------------

HEADER = "X-Request-ID"
NO_REQUEST = "-"                                       # outside a request, such as at startup
FORMAT = "%(asctime)s %(levelname)s %(service)s request_id=%(request_id)s %(name)s: %(message)s"

request_id: ContextVar[str] = ContextVar("request_id", default=NO_REQUEST)

_VALID_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_SHAPES = (
    re.compile(r"\beyJ[\w-]*\.[\w-]+\.[\w-]*"),       # a JWT
    re.compile(r"(?i)\bbearer\s+[^\s,;'\"]+"),         # an Authorization header's value
    re.compile(r"\b(?:sk|pk)-[\w-]{8,}"),              # an OpenAI or LangFuse key
    TIN_PATTERN,                                        # a full SSN or EIN
)
_HANDLER = "app-log"
_UVICORN = ("uvicorn", "uvicorn.error", "uvicorn.access")
_HEALTH_CHECK = re.compile(r'"GET /healthz? HTTP/')


def valid_request_id(value: str | None) -> str | None:
    """value, if it can be a request ID (letters, digits and hyphens, 64 at most); else None."""
    return value if value and _VALID_ID.fullmatch(value) else None


def safe(value: object, limit: int = 200) -> str:
    """Something a client sent, fit for one line: control characters replaced, and cut to limit."""
    text = "".join(c if c.isprintable() else "?" for c in str(value))
    return text if len(text) <= limit else text[:limit] + "…"


def level(name: str | None) -> int:
    """The level LOG_LEVEL names (DEBUG, INFO, WARNING, ERROR, CRITICAL); INFO if unset or unknown."""
    return logging.getLevelNamesMapping().get((name or "").strip().upper(), logging.INFO)


class Formatter(logging.Formatter):
    """FORMAT with the time in UTC, and secrets blanked out of the message and any traceback."""

    def __init__(self, secrets=()):
        super().__init__(FORMAT)
        self.secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def formatTime(self, record, datefmt=None):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z"

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        for shape in _SHAPES:
            text = shape.sub("[redacted]", text)
        return text

    def format(self, record: logging.LogRecord) -> str:
        copy = logging.makeLogRecord(record.__dict__)     # other handlers keep the original
        copy.msg, copy.args = self.redact(record.getMessage()), None
        if copy.exc_info:
            copy.exc_text = self.redact(copy.exc_text or self.formatException(copy.exc_info))
        return super().format(copy)


def _with_request(previous):
    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.service, record.request_id = SERVICE, request_id.get()
        return record
    factory.app_log = True
    return factory


def not_a_health_check(record: logging.LogRecord) -> bool:
    """False for uvicorn's access line of a health check, which is then left out."""
    return not _HEALTH_CHECK.search(record.getMessage())


def configure() -> None:
    """Sends every line to stderr in FORMAT, at LOG_LEVEL, uvicorn's too. Calling it again
    changes nothing, and handlers others added, such as pytest's, stay.

    It runs when app.main is imported, which uvicorn does after setting up its own
    logging, so the handlers uvicorn gave its loggers are replaced by this one."""
    if not getattr(logging.getLogRecordFactory(), "app_log", False):
        logging.setLogRecordFactory(_with_request(logging.getLogRecordFactory()))
    root = logging.getLogger()
    if not any(handler.get_name() == _HANDLER for handler in root.handlers):
        handler = logging.StreamHandler()
        handler.set_name(_HANDLER)
        handler.setFormatter(Formatter([os.environ.get(name) for name in SECRETS]))
        root.addHandler(handler)
    chosen = level(os.environ.get("LOG_LEVEL"))
    root.setLevel(chosen)
    for name in _UVICORN:
        logger = logging.getLogger(name)
        logger.handlers.clear()       # uvicorn's format has no time, service or request ID
        logger.propagate = True
        logger.setLevel(chosen)
    access = logging.getLogger("uvicorn.access")
    if not_a_health_check not in access.filters:
        access.addFilter(not_a_health_check)
    for name in QUIET:
        logging.getLogger(name).setLevel(max(chosen, logging.WARNING))


def track_requests(app: FastAPI) -> None:
    """Puts each request's X-Request-ID, as the gateway set it, on the lines logged while
    handling it. A request without a valid one is logged as NO_REQUEST."""

    @app.middleware("http")
    async def request_ids(request: Request, call_next):
        # Not reset afterwards: uvicorn runs each request in a task of its own, and logs its
        # access line, or an unhandled error, after this returns, in the same task.
        request_id.set(valid_request_id(request.headers.get(HEADER)) or NO_REQUEST)
        return await call_next(request)
