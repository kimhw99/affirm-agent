"""The API gateway: the only entry point for the browser (docs/auth.md).

It routes /auth to the auth service, /cases and /metrics to the case service, and
/reviews to the agent service. /auth passes through as it is, since the auth service checks its own
credentials. Every other request needs a JWT from the auth service. The gateway
verifies it, drops any X-User-* header the client sent, and forwards who is acting as
X-User-Id and X-User-Role, without the JWT itself. Requests to the case service carry
the service token (CASE_SERVICE_TOKEN), and only reads get through: the decision is
saved by the agent service, never straight from the browser.

Settings: JWT_SECRET (the auth service's, at least 32 characters), CASE_SERVICE_TOKEN,
and the services' base URLs AUTH_SERVICE_URL, CASE_SERVICE_URL and AGENT_SERVICE_URL
(by default http://auth:8000, http://case:8000 and http://agent:8000, as in Docker
Compose).
"""

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import ValidationError

from kyb_contracts.auth import ROLES, USER_ID_HEADER, USER_ROLE_HEADER, TokenClaims


@dataclass(frozen=True)
class Route:
    url_variable: str       # the environment variable with the service's base URL
    default_url: str
    methods: frozenset[str]
    needs_user: bool        # the request needs a valid JWT
    service_token: bool     # the service gets CASE_SERVICE_TOKEN


# The first segment of the path picks the route.
ROUTES: dict[str, Route] = {
    "auth": Route("AUTH_SERVICE_URL", "http://auth:8000", frozenset({"GET", "POST"}),
                  needs_user=False, service_token=False),
    "cases": Route("CASE_SERVICE_URL", "http://case:8000", frozenset({"GET"}),
                   needs_user=True, service_token=True),
    "metrics": Route("CASE_SERVICE_URL", "http://case:8000", frozenset({"GET"}),
                     needs_user=True, service_token=True),
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


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """The gateway. Tests pass settings and a fake transport; otherwise both come from the environment."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings or Settings.from_env()
        async with httpx.AsyncClient(transport=transport, timeout=30.0) as client:
            app.state.client = client
            yield

    app = FastAPI(title="API gateway", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def forward(path: str, request: Request) -> Response:
        name = path.split("/", 1)[0]
        route = ROUTES.get(name)
        if route is None:
            raise HTTPException(404, "not found")
        if request.method not in route.methods:
            raise HTTPException(405, f"{request.method} isn't allowed on /{name}")
        current: Settings = request.app.state.settings
        headers = {key: value for key, value in request.headers.items()
                   if key not in HOP_BY_HOP and not key.startswith("x-user-")}
        if route.needs_user:
            claims = verify(headers.pop("authorization", None), current.jwt_secret)
            headers[USER_ID_HEADER], headers[USER_ROLE_HEADER] = claims.sub, claims.role
        if route.service_token:
            headers["authorization"] = f"Bearer {current.service_token}"
        try:
            upstream = await request.app.state.client.request(
                request.method, f"{current.urls[name]}/{path}", params=list(request.query_params.multi_items()),
                content=await request.body(), headers=headers)
        except httpx.HTTPError:
            raise HTTPException(502, f"the {name} service isn't reachable") from None
        # httpx has already decoded the body, so its encoding and length headers no longer apply.
        passed = {key: value for key, value in upstream.headers.items()
                  if key.lower() not in HOP_BY_HOP | {"content-encoding"}}
        return Response(upstream.content, status_code=upstream.status_code, headers=passed)

    return app


app = create_app()
