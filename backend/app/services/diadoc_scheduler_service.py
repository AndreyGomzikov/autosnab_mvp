from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.db.session import SessionLocal
from app.services.diadoc_sync_service import sync_diadoc_documents

logger = logging.getLogger(__name__)

_STOP_EVENT = threading.Event()
_THREAD: threading.Thread | None = None
_THREAD_LOCK = threading.Lock()
_STATUS_LOCK = threading.Lock()
_LAST_RUN_AT: str | None = None
_LAST_SUCCESS_AT: str | None = None
_LAST_ERROR: str | None = None
_LAST_RESULT: dict[str, Any] | None = None


def start_diadoc_scheduler() -> bool:
    global _THREAD
    if not _scheduler_configuration_ready():
        return False
    with _THREAD_LOCK:
        if _THREAD and _THREAD.is_alive():
            return True
        _STOP_EVENT.clear()
        _THREAD = threading.Thread(
            target=_scheduler_loop,
            name="diadoc-sync-scheduler",
            daemon=True,
        )
        _THREAD.start()
    return True


def stop_diadoc_scheduler() -> None:
    global _THREAD
    _STOP_EVENT.set()
    with _THREAD_LOCK:
        thread = _THREAD
        _THREAD = None
    if thread and thread.is_alive():
        thread.join(timeout=5)


def diadoc_scheduler_status() -> dict[str, object]:
    with _STATUS_LOCK:
        return {
            "enabled": settings.diadoc_scheduler_enabled,
            "configured": _scheduler_configuration_ready(),
            "running": bool(_THREAD and _THREAD.is_alive()),
            "interval_seconds": settings.diadoc_sync_interval_seconds,
            "last_run_at": _LAST_RUN_AT,
            "last_success_at": _LAST_SUCCESS_AT,
            "last_error": _LAST_ERROR,
            "last_result": dict(_LAST_RESULT or {}),
        }


def _scheduler_configuration_ready() -> bool:
    auth_configured = bool(
        settings.diadoc_access_token
        or (
            settings.diadoc_refresh_token
            and settings.diadoc_client_id
            and settings.diadoc_client_secret
        )
    )
    return bool(
        settings.diadoc_integration_enabled
        and settings.diadoc_scheduler_enabled
        and settings.diadoc_box_id
        and auth_configured
    )


def _scheduler_loop() -> None:
    interval = max(30, settings.diadoc_sync_interval_seconds)
    while not _STOP_EVENT.is_set():
        _run_once()
        _STOP_EVENT.wait(interval)


def _run_once() -> None:
    global _LAST_ERROR
    global _LAST_RESULT
    global _LAST_RUN_AT
    global _LAST_SUCCESS_AT

    run_at = datetime.now(timezone.utc).isoformat()
    db = SessionLocal()
    try:
        result = sync_diadoc_documents(
            db,
            create_google_sheet=True,
        )
        with _STATUS_LOCK:
            _LAST_RUN_AT = run_at
            _LAST_SUCCESS_AT = datetime.now(
                timezone.utc
            ).isoformat()
            _LAST_ERROR = None
            _LAST_RESULT = dict(result)
    except Exception as exc:  # noqa: BLE001 - scheduler survives transient failures
        with _STATUS_LOCK:
            _LAST_RUN_AT = run_at
            _LAST_ERROR = str(exc)
            _LAST_RESULT = None
        logger.exception(
            "Automatic Diadoc synchronization failed"
        )
    finally:
        db.close()
