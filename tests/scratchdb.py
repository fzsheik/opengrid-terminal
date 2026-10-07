"""A throwaway database for tests: created and dropped over SQL, so no Postgres CLI is needed.

The server comes from TEST_PG_URL (default: localhost, current OS user), never the app's database.
"""

import os

from sqlalchemy import create_engine, text

SERVER = os.environ.get("TEST_PG_URL", "postgresql+psycopg://localhost:5432")


def url(name: str) -> str:
    return f"{SERVER}/{name}"


def _admin(sql: str) -> None:
    engine = create_engine(url("postgres"), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(sql))
    finally:
        engine.dispose()


def drop(name: str) -> None:
    _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def create(name: str) -> str:
    """Drop and recreate `name`; returns its URL."""
    drop(name)
    _admin(f'CREATE DATABASE "{name}"')
    return url(name)
