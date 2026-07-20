from app.services import provider_health_service


def test_provider_health_marks_openai_missing_key(monkeypatch):
    monkeypatch.setattr(provider_health_service.settings, "openai_api_key", None)

    health = provider_health_service.provider_health()

    assert health["openai"]["ready"] is False
    assert "OPENAI_API_KEY" in health["openai"]["reason"]


def test_provider_health_reports_cv2_import_failure(monkeypatch):
    monkeypatch.setattr(
        provider_health_service.importlib.util,
        "find_spec",
        lambda name: object() if name == "mineru" else None,
    )
    original_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "cv2":
            raise ImportError("libxcb.so.1 missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    health = provider_health_service._mineru_health()

    assert health["ready"] is False
    assert "libxcb.so.1" in health["reason"]


def test_provider_health_reports_incomplete_mineru_model_cache(monkeypatch, tmp_path):
    snapshot_root = tmp_path / "snapshot-a"
    snapshot_root.mkdir()
    monkeypatch.setattr(
        provider_health_service.importlib.util,
        "find_spec",
        lambda name: object() if name == "mineru" else None,
    )
    monkeypatch.setattr(
        provider_health_service,
        "_mineru_snapshot_roots",
        lambda: [snapshot_root],
    )
    original_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "cv2":
            return object()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    health = provider_health_service.mineru_health()

    assert health["ready"] is False
    assert "MinerU model cache is incomplete" in health["reason"]
    assert "unet.onnx" in health["reason"]
