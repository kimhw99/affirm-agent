"""Auth formats (BEAC-11): the roles, the login request and response, and the JWT's claims.

The auth service checks a username and password and issues a JWT. The gateway verifies
that JWT on every request to /cases and /reviews, drops any X-User-* header the client
sent, and forwards who is acting as X-User-Id and X-User-Role. The backend services
read those two headers and never see the JWT. docs/auth.md has the details.
"""

from typing import Literal

from pydantic import Field

from .case_input import Contract

# Who can use the review app. Both roles can open cases and reviews and decide them; the
# case queue shows an analyst the cases waiting for review and a senior analyst the
# escalated ones.
Role = Literal["analyst", "senior_analyst"]
ROLES: frozenset[str] = frozenset({"analyst", "senior_analyst"})

# The headers the gateway sets on every request it forwards to /cases and /reviews.
USER_ID_HEADER = "X-User-Id"
USER_ROLE_HEADER = "X-User-Role"


class LoginRequest(Contract):
    """Body of POST /auth/login."""

    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class User(Contract):
    """A review-app user, as POST /auth/login and GET /auth/me return it. Never the password."""

    user_id: str                                  # u_analyst_01; becomes analyst_id in DecisionRecord
    username: str
    display_name: str
    role: Role


class TokenClaims(Contract):
    """The JWT's payload, signed with HS256 and the JWT_SECRET the auth service and gateway share."""

    sub: str                                      # the user's user_id
    role: Role
    name: str                                     # the display name, for the review app
    iat: int                                      # issued at, Unix seconds
    exp: int                                      # expires at, Unix seconds


class LoginResponse(Contract):
    """Response of POST /auth/login."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int                               # seconds until exp
    user: User
