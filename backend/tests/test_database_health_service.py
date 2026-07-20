from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.services import database_health_service


def test_database_health_reports_missing_sqlite_directory(tmp_path):
    missing_dir = tmp_path / "missing"
    database_url = f"sqlite:///{missing_dir / 'autosnab_mvp.db'}"
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    health = database_health_service.database_health(database_url, target_engine=engine)

    assert health["ready"] is False
    assert "does not exist" in health["reason"]


def test_database_health_reports_memory_sqlite_as_ready():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    health = database_health_service.database_health("sqlite:///:memory:", target_engine=engine)

    assert health == {"ready": True, "reason": None, "location": ":memory:"}


def test_describe_database_write_error_mentions_sqlite_location(tmp_path):
    path = Path(tmp_path / "autosnab_mvp.db")

    message = database_health_service.describe_database_write_error(
        Exception("attempt to write a readonly database"),
        f"sqlite:///{path}",
    )

    assert message is not None
    assert "read-only" in message
    assert str(path) in message
