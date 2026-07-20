import importlib.util
from pathlib import Path
from typing import Any

from app.config import settings

_MINERU_REQUIRED_MODEL_SUFFIXES = (
    "models/TabRec/UnetStructure/unet.onnx",
)


def provider_health() -> dict[str, Any]:
    return {
        "mineru": mineru_health(),
        "google_ocr": _google_ocr_health(),
        "openai": _openai_health(),
    }


def mineru_health() -> dict[str, Any]:
    if importlib.util.find_spec("mineru") is None:
        return {"ready": False, "reason": "mineru package is not installed"}
    try:
        import cv2  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - native import failures must be visible
        return {"ready": False, "reason": f"OpenCV import failed: {exc}"}
    missing_models = _missing_mineru_models()
    if missing_models:
        missing = ", ".join(missing_models)
        return {"ready": False, "reason": f"MinerU model cache is incomplete: missing {missing}"}
    return {"ready": True, "reason": None}


def _google_ocr_health() -> dict[str, Any]:
    if not settings.google_drive_ocr_enabled:
        return {"ready": False, "reason": "GOOGLE_DRIVE_OCR_ENABLED is false"}
    required = {
        "GOOGLE_OAUTH_CLIENT_ID": settings.google_oauth_client_id,
        "GOOGLE_OAUTH_CLIENT_SECRET": settings.google_oauth_client_secret,
        "GOOGLE_OAUTH_REFRESH_TOKEN": settings.google_oauth_refresh_token,
    }
    missing = [name for name, value in required.items() if not value]
    return {
        "ready": not missing,
        "reason": f"Missing settings: {', '.join(missing)}" if missing else None,
    }


def _openai_health() -> dict[str, Any]:
    return {
        "ready": bool(settings.openai_api_key),
        "reason": None if settings.openai_api_key else "OPENAI_API_KEY is not configured",
        "model": settings.openai_invoice_model,
    }


def _mineru_health() -> dict[str, Any]:
    return mineru_health()


def _missing_mineru_models() -> list[str]:
    snapshot_roots = sorted(_mineru_snapshot_roots())
    if not snapshot_roots:
        return list(_MINERU_REQUIRED_MODEL_SUFFIXES)
    missing: list[str] = []
    for suffix in _MINERU_REQUIRED_MODEL_SUFFIXES:
        if not any((root / suffix).is_file() for root in snapshot_roots):
            missing.append(suffix)
    return missing


def _mineru_snapshot_roots() -> list[Path]:
    cache_root = Path.home() / ".cache" / "huggingface" / "hub" / "models--opendatalab--PDF-Extract-Kit-1.0" / "snapshots"
    if not cache_root.is_dir():
        return []
    return [path for path in cache_root.iterdir() if path.is_dir()]
