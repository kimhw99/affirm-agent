"""Every read and write of auth_db, in one place: the tests replace these functions,
so they run without a database (tests/test_auth.py).

Each function takes a SQLAlchemy Session and works with the ORM model in app/db.py.
"""

import os

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from . import db
from .security import hash_password

# The demo users: user_id, username, display name, role, and the variable with the password.
DEMO_USERS = [
    ("u_analyst_01", "analyst", "Demo Analyst", "analyst", "DEMO_ANALYST_PASSWORD"),
    ("u_senior_01", "senior", "Demo Senior Analyst", "senior_analyst", "DEMO_SENIOR_PASSWORD"),
]
UPDATED_ON_CONFLICT = ("username", "display_name", "role", "password_hash")


def find_user_by_username(session: Session, username: str) -> db.User | None:
    """The user with this username, with the password hash a login is checked against."""
    return session.scalars(select(db.User).where(db.User.username == username)).one_or_none()


def find_user_by_id(session: Session, user_id: str) -> db.User | None:
    """The user a token's sub names, or None if it no longer exists."""
    return session.get(db.User, user_id)


def save_demo_users(session: Session) -> None:
    """Creates each demo user whose password is set, or updates it to match the environment."""
    rows = [{"user_id": user_id, "username": username, "display_name": name, "role": role,
             "password_hash": hash_password(password)}
            for user_id, username, name, role, variable in DEMO_USERS if (password := os.environ.get(variable))]
    if not rows:
        return
    statement = insert(db.User).values(rows)
    session.execute(statement.on_conflict_do_update(
        index_elements=["user_id"], set_={column: statement.excluded[column] for column in UPDATED_ON_CONFLICT}))
