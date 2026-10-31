from __future__ import annotations

import logging
from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _make_engine():
    settings = get_settings()
    # WSL2 / SQLite: use check_same_thread=False for FastAPI background tasks
    is_sqlite = settings.database_url.startswith("sqlite")
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    eng = create_engine(
        settings.database_url,
        connect_args=connect_args,
        echo=False,
    )
    if is_sqlite:
        # SQLite ignores foreign keys unless enabled per connection — without
        # this, deleting a story left orphaned translations/parts/jobs behind.
        @event.listens_for(eng, "connect")
        def _enable_foreign_keys(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return eng


engine = _make_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _add_missing_columns() -> None:
    """
    Minimal forward-only migration: create_all() never alters existing tables,
    so add columns that exist on the models but not yet in the database.
    Only nullable columns or columns with a server_default can be added.
    """
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = (
                    f"ALTER TABLE {table.name} ADD COLUMN {column.name} "
                    f"{column.type.compile(engine.dialect)}"
                )
                if column.server_default is not None:
                    ddl += f" DEFAULT {column.server_default.arg!r}"
                elif not column.nullable:
                    logger.error(
                        "Cannot add NOT NULL column %s.%s without a server_default — skipped.",
                        table.name, column.name,
                    )
                    continue
                conn.execute(text(ddl))
                logger.info("Migration: added column %s.%s", table.name, column.name)


def init_db() -> None:
    """Create all tables and add missing columns. Called at startup."""
    # Import models so Base.metadata is populated before create_all
    import app.models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency — yields a DB session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
