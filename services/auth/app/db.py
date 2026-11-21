"""auth_db through SQLAlchemy 2.x: the engine, and the ORM model of the users table.

The model mirrors services/auth/schema.sql: the same columns, types and keys.
schema.sql still creates the table, so the service never calls metadata.create_all,
and a change to the table goes into both files (tests/test_auth.py checks that they
agree). The role check lives only in schema.sql. The driver is psycopg 3
(postgresql+psycopg://).
"""

import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import URL, DateTime, Engine, Text, create_engine, func, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

POOL_SIZE = 5        # connections, as many as psycopg_pool's max_size before
STARTUP_WAIT = 30    # seconds to wait for the database when the service starts


def make_engine(*, host: str, port: str | int, password: str) -> Engine:
    """The engine for auth_db, as auth_svc: at most POOL_SIZE connections, each checked before use."""
    url = URL.create("postgresql+psycopg", username="auth_svc", password=password, host=host, port=int(port),
                     database="auth_db")
    return create_engine(url, pool_size=POOL_SIZE, max_overflow=0, pool_pre_ping=True)


def wait_for_database(engine: Engine, timeout: float = STARTUP_WAIT) -> None:
    """Connects once, trying again for up to timeout seconds while the database starts."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


def run_schema(engine: Engine, schema: Path) -> None:
    """Runs schema.sql in one go. psycopg runs several statements in one call only when
    there are no parameters, so the script goes to the driver's own connection."""
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cursor:
            cursor.execute(schema.read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()


class Base(DeclarativeBase):
    pass


class User(Base):
    """A user who can log in. Not the API's User (kyb_contracts.auth), which never has the hash."""

    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)      # scrypt; never returned
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
