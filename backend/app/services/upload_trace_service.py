from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any


_TRACE_TTL = timedelta(hours=2)
_traces: dict[str, dict[str, Any]] = {}
_lock = Lock()


def initialize_trace(trace_id: str) -> None:
    with _lock:
        _cleanup_expired_locked()
        _traces[trace_id] = {
            "trace_id": trace_id,
            "trace_version": "1.0",
            "logs": [],
            "completed": False,
            "error_message": None,
            "result": None,
            "metadata": {},
            "updated_at": _now(),
        }


def append_trace_log(trace_id: str, log: dict[str, Any]) -> None:
    with _lock:
        trace = _traces.setdefault(
            trace_id,
            {
                "trace_id": trace_id,
                "trace_version": "1.0",
                "logs": [],
                "completed": False,
                "error_message": None,
                "result": None,
                "metadata": {},
                "updated_at": _now(),
            },
        )
        trace["logs"].append(log)
        trace["updated_at"] = _now()


def finalize_trace(trace_id: str, *, error_message: str | None = None) -> None:
    with _lock:
        trace = _traces.setdefault(
            trace_id,
            {
                "trace_id": trace_id,
                "trace_version": "1.0",
                "logs": [],
                "completed": False,
                "error_message": None,
                "result": None,
                "metadata": {},
                "updated_at": _now(),
            },
        )
        trace["completed"] = True
        trace["error_message"] = error_message
        trace["updated_at"] = _now()


def set_trace_result(trace_id: str, result: dict[str, Any] | None) -> None:
    with _lock:
        trace = _traces.setdefault(
            trace_id,
            {
                "trace_id": trace_id,
                "trace_version": "1.0",
                "logs": [],
                "completed": False,
                "error_message": None,
                "result": None,
                "metadata": {},
                "updated_at": _now(),
            },
        )
        trace["result"] = result
        trace["updated_at"] = _now()


def set_trace_metadata(trace_id: str, **metadata: Any) -> None:
    with _lock:
        trace = _traces.setdefault(
            trace_id,
            {
                "trace_id": trace_id,
                "trace_version": "1.0",
                "logs": [],
                "completed": False,
                "error_message": None,
                "result": None,
                "metadata": {},
                "updated_at": _now(),
            },
        )
        trace["metadata"].update({key: value for key, value in metadata.items() if value is not None})
        trace["updated_at"] = _now()


def get_trace(trace_id: str) -> dict[str, Any] | None:
    with _lock:
        _cleanup_expired_locked()
        trace = _traces.get(trace_id)
        if trace is None:
            return None
        return {
            "trace_id": trace["trace_id"],
            "trace_version": trace["trace_version"],
            "logs": list(trace["logs"]),
            "completed": bool(trace["completed"]),
            "error_message": trace["error_message"],
            "result": trace["result"],
            "metadata": dict(trace["metadata"]),
            "updated_at": trace["updated_at"].isoformat(),
        }


def _cleanup_expired_locked() -> None:
    cutoff = _now() - _TRACE_TTL
    expired = [trace_id for trace_id, trace in _traces.items() if trace["updated_at"] < cutoff]
    for trace_id in expired:
        _traces.pop(trace_id, None)


def _now() -> datetime:
    return datetime.now(timezone.utc)
