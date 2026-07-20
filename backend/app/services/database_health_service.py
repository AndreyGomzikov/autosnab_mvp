import os
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings

_SQLITE_WRITE_PROBE_TABLE = "__autosnab_write_probe"


def database_health(
    database_url: str | None = None,
    *,
    target_engine: Engine | None = None,
) -> dict[str, Any]:
    database_url = database_url or settings.database_url
    target_engine = target_engine or _require_engine()

    if not database_url.startswith("sqlite"):
        return _generic_database_health(target_engine, database_url)

    try:
        database_path = _sqlite_database_path(database_url)
    except ValueError as exc:
        return {"ready": False, "reason": str(exc), "location": None}

    if database_path is None:
        return {"ready": True, "reason": None, "location": ":memory:"}

    parent = database_path.parent
    if not parent.exists():
        return {"ready": False, "reason": f"SQLite directory does not exist: {parent}", "location": str(database_path)}
    if not os.access(parent, os.W_OK):
        return {
            "ready": False,
            "reason": f"SQLite directory is not writable: {parent}",
            "location": str(database_path),
        }
    if database_path.exists() and not os.access(database_path, os.W_OK):
        return {
            "ready": False,
            "reason": f"SQLite database file is not writable: {database_path}",
            "location": str(database_path),
        }

    try:
        with target_engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except SQLAlchemyError as exc:
        return {"ready": False, "reason": f"Database connectivity check failed: {exc}", "location": str(database_path)}

    return {"ready": True, "reason": None, "location": str(database_path)}


def assert_database_writable(
    *,
    database_url: str | None = None,
    target_engine: Engine | None = None,
) -> None:
    database_url = database_url or settings.database_url
    target_engine = target_engine or _require_engine()

    health = database_health(database_url, target_engine=target_engine)
    if not health["ready"]:
        raise RuntimeError(str(health["reason"]))

    if database_url.startswith("sqlite"):
        with target_engine.begin() as conn:
            conn.exec_driver_sql(
                f"CREATE TABLE IF NOT EXISTS {_SQLITE_WRITE_PROBE_TABLE} ("
                "id INTEGER PRIMARY KEY, touched_at TEXT NOT NULL)"
            )
            conn.exec_driver_sql(
                f"INSERT INTO {_SQLITE_WRITE_PROBE_TABLE}(touched_at) VALUES (CURRENT_TIMESTAMP)"
            )
            conn.exec_driver_sql(
                f"DELETE FROM {_SQLITE_WRITE_PROBE_TABLE} WHERE id = last_insert_rowid()"
            )
        return

    with target_engine.connect() as conn:
        conn.exec_driver_sql("SELECT 1")


def describe_database_write_error(exc: Exception, database_url: str | None = None) -> str | None:
    message = str(exc)
    if "attempt to write a readonly database" not in message.lower():
        return None

    database_url = database_url or settings.database_url
    try:
        database_path = _sqlite_database_path(database_url)
    except ValueError:
        database_path = None

    location = str(database_path) if database_path is not None else database_url
    return (
        "SQLite database is read-only. "
        f"Check write permissions for the database file and its parent directory/volume: {location}"
    )


def _generic_database_health(target_engine: Engine, database_url: str) -> dict[str, Any]:
    try:
        with target_engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except SQLAlchemyError as exc:
        return {"ready": False, "reason": f"Database connectivity check failed: {exc}", "location": database_url}
    return {"ready": True, "reason": None, "location": database_url}


def _sqlite_database_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    database = url.database
    if database in (None, "", ":memory:"):
        return None
    return Path(database)


def _require_engine() -> Engine:
    from app.db.session import engine

    return engine
