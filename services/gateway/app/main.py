"""The API gateway: the only entry point for the browser (docs/auth.md).

It routes /auth to the auth service, /cases and /metrics to the case service, and
/reviews to the agent service. /auth passes through as it is, since the auth service checks its own
credentials. Every other request needs a JWT from the auth service. The gateway
verifies it, drops any X-User-* header the client sent, and forwards who is acting as
X-User-Id and X-User-Role, without the JWT itself. Requests to the case service carry
the service token (CASE_SERVICE_TOKEN). Only reads get through to it, and one write:
POST /cases/{case_id}/documents, where an analyst adds the documents the merchant sent
for a docs_requested case. A decision is saved by the agent service, never straight
from the browser, so POST /cases/{case_id}/decisions is 405 here.

A route can be limited to some roles: /metrics is for senior analysts only, so an analyst
gets 403. Who may review and decide which case depends on the case's status, so the agent
service checks that (docs/auth.md, Roles).

Every request gets an X-Request-ID: the client's, if it's valid, or a new one. The
gateway passes it to the service it forwards to and returns it in the response, and
logs one line per request (app/logs.py): the method, the path, the service it went to,
the status, how long it took, and who asked. Never the Authorization header, a token
or the body. A refused token (401, 403) is a warning, a service out of reach (502) an
error.

Settings: JWT_SECRET (the auth service's, at least 32 characters), CASE_SERVICE_TOKEN,
the services' base URLs AUTH_SERVICE_URL, CASE_SERVICE_URL and AGENT_SERVICE_URL (by
default http://auth:8000, http://case:8000 and http://agent:8000, as in Docker
Compose), and LOG_LEVEL.
"""

import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import ValidationError

from kyb_contracts.auth import ROLES, USER_ID_HEADER, USER_ROLE_HEADER, TokenClaims

from . import logs

logs.configure()
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Route:
    url_variable: str       # the environment variable with the service's base URL
    default_url: str
    methods: frozenset[str]
    needs_user: bool        # the request needs a valid JWT
    service_token: bool     # the service gets CASE_SERVICE_TOKEN
    roles: frozenset[str] | None = None     # only these roles (403 for the others); None: any role
    post_paths: tuple[str, ...] = ()        # paths (regular expressions) that also take POST

    def allows(self, method: str, path: str) -> bool:
        return method in self.methods or (method == "POST" and any(re.fullmatch(p, path) for p in self.post_paths))

    @property
    def service(self) -> str:
        """The service it goes to, as the logs name it: auth, case or agent."""
        return self.url_variable.removesuffix("_SERVICE_URL").lower()


# The first segment of the path picks the route.
ROUTES: dict[str, Route] = {
    "auth": Route("AUTH_SERVICE_URL", "http://auth:8000", frozenset({"GET", "POST"}),
                  needs_user=False, service_token=False),
    "cases": Route("CASE_SERVICE_URL", "http://case:8000", frozenset({"GET"}),
                   needs_user=True, service_token=True, post_paths=(r"cases/[^/]+/documents",)),
    "metrics": Route("CASE_SERVICE_URL", "http://case:8000", frozenset({"GET"}),
                     needs_user=True, service_token=True, roles=frozenset({"senior_analyst"})),
    "reviews": Route("AGENT_SERVICE_URL", "http://agent:8000", frozenset({"GET", "POST"}),
                     needs_user=True, service_token=False),
}

# Headers that describe one hop rather than the request, so they aren't passed along.
HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
                        "trailer", "transfer-encoding", "upgrade", "host", "content-length"})


@dataclass(frozen=True)
class Settings:
    jwt_secret: str
    service_token: str
    urls: dict[str, str]

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.environ.get("JWT_SECRET", "")
        if len(secret) < 32:
            raise RuntimeError("JWT_SECRET must be at least 32 characters (see .env.example)")
        token = os.environ.get("CASE_SERVICE_TOKEN")
        if not token:
            raise RuntimeError("set CASE_SERVICE_TOKEN (see .env.example)")
        urls = {name: (os.environ.get(route.url_variable) or route.default_url).rstrip("/")
                for name, route in ROUTES.items()}
        return cls(secret, token, urls)


def verify(authorization: str | None, secret: str) -> TokenClaims:
    """The claims of a valid Bearer JWT: 401 if it's missing, invalid or expired, 403 for another role."""
    challenge = {"WWW-Authenticate": "Bearer"}
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "log in first", headers=challenge)
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["sub", "iat", "exp"]})
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "the session has expired; log in again", headers=challenge) from None
    except jwt.InvalidTokenError:
        raise HTTPException(401, "invalid token", headers=challenge) from None
    if payload.get("role") not in ROLES:
        raise HTTPException(403, "this role can't use the review app")
    try:
        return TokenClaims.model_validate(payload)
    except ValidationError:
        raise HTTPException(401, "invalid token", headers=challenge) from None


def log_request(request: Request, status: int, started: float) -> None:
    """The request's one line: info, a warning for a refused token (401, 403), an error from 500 on."""
    if request.url.path == "/health":
        return                     # Docker Compose asks every few seconds
    state = request.state
    reason = getattr(state, "reason", None)
    level = logging.ERROR if status >= 500 else logging.WARNING if status in (401, 403) else logging.INFO
    log.log(level, "method=%s path=%s upstream=%s status=%d duration_ms=%d user_id=%s role=%s%s",
            request.method, logs.safe(request.url.path), getattr(state, "upstream", "-"), status,
            round((time.perf_counter() - started) * 1000), getattr(state, "user_id", "-"),
            getattr(state, "role", "-"), f" reason={reason!r}" if reason else "")


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """The gateway. Tests pass settings and a fake transport; otherwise both come from the environment."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings or Settings.from_env()
        async with httpx.AsyncClient(transport=transport, timeout=30.0) as client:
            app.state.client = client
            yield

    app = FastAPI(title="API gateway", lifespan=lifespan)

    @app.middleware("http")
    async def request_ids_and_lines(request: Request, call_next) -> Response:
        rid = logs.valid_request_id(request.headers.get(logs.HEADER)) or str(uuid.uuid4())
        logs.request_id.set(rid)           # not reset, as in logs.track_requests
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log_request(request, 500, started)     # uvicorn then logs the traceback, with the same ID
            raise
        response.headers[logs.HEADER] = rid
        log_request(request, response.status_code, started)
        return response

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    def refuse(request: Request, status: int, detail: str) -> HTTPException:
        request.state.reason = detail
        return HTTPException(status, detail)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def forward(path: str, request: Request) -> Response:
        name = path.split("/", 1)[0]
        route = ROUTES.get(name)
        if route is None:
            raise refuse(request, 404, "not found")
        if not route.allows(request.method, path):
            raise refuse(request, 405, f"{request.method} isn't allowed on /{path}")
        request.state.upstream = route.service
        current: Settings = request.app.state.settings
        headers = {key: value for key, value in request.headers.items()
                   if key not in HOP_BY_HOP and not key.startswith("x-user-") and key != "x-request-id"}
        headers[logs.HEADER] = logs.request_id.get()
        if route.needs_user:
            try:
                claims = verify(headers.pop("authorization", None), current.jwt_secret)
            except HTTPException as refused:
                request.state.reason = refused.detail
                raise
            request.state.user_id, request.state.role = claims.sub, claims.role
            if route.roles is not None and claims.role not in route.roles:
                raise refuse(request, 403, f"/{name} is only for {' or '.join(sorted(route.roles))}")
            headers[USER_ID_HEADER], headers[USER_ROLE_HEADER] = claims.sub, claims.role
        if route.service_token:
            headers["authorization"] = f"Bearer {current.service_token}"
        try:
            upstream = await request.app.state.client.request(
                request.method, f"{current.urls[name]}/{path}", params=list(request.query_params.multi_items()),
                content=await request.body(), headers=headers)
        except httpx.HTTPError:
            raise refuse(request, 502, f"the {name} service isn't reachable") from None
        # httpx has already decoded the body, so its encoding and length headers no longer apply.
        passed = {key: value for key, value in upstream.headers.items()
                  if key.lower() not in HOP_BY_HOP | {"content-encoding"}}
        return Response(upstream.content, status_code=upstream.status_code, headers=passed)

    return app


app = create_app()
