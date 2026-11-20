"""Password hashing (scrypt, from the standard library) and JWTs (HS256, with PyJWT)."""

import base64
import hashlib
import hmac
import os
import time

import jwt

from kyb_contracts.auth import TokenClaims, User

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _unb64(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def hash_password(password: str) -> str:
    """scrypt with a random salt, stored as scrypt$n$r$p$salt$hash."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        expected = _unb64(digest)
        actual = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
    except (ValueError, TypeError):  # not a hash this function wrote
        return False
    return scheme == "scrypt" and hmac.compare_digest(actual, expected)


# Checked when no user has the given name, so a wrong username takes as long as a wrong password.
DUMMY_HASH = hash_password("no user has this password")


def issue_token(user: User, secret: str, ttl_seconds: int, now: int | None = None) -> str:
    issued = int(time.time()) if now is None else now
    claims = TokenClaims(sub=user.user_id, role=user.role, name=user.display_name,
                         iat=issued, exp=issued + ttl_seconds)
    return jwt.encode(claims.model_dump(), secret, algorithm="HS256")


def read_token(token: str, secret: str) -> TokenClaims:
    """The token's claims. Raises jwt.InvalidTokenError (expired, tampered with, malformed)
    or pydantic.ValidationError (claims that aren't TokenClaims)."""
    payload = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["sub", "iat", "exp"]})
    return TokenClaims.model_validate(payload)
