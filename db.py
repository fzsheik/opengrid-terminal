"""Engine, sessions, and schema versioning.

Alembic owns the schema: nothing here calls create_all, so the migrations are
the only description of what the database looks like.
"""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import settings

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db():
    with SessionLocal() as session:
        yield session


def _alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def current_revision() -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def head_revision() -> str | None:
    return ScriptDirectory.from_config(_alembic_config()).get_current_head()


def init_db() -> None:
    """Bring the database up to the latest migration if it is behind."""
    current, head = current_revision(), head_revision()
    if current == head:
        return
    log.info("migrating database: %s -> %s", current or "(empty)", head)
    command.upgrade(_alembic_config(), "head")
