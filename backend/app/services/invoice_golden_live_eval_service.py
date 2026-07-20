from __future__ import annotations

from typing import Any, Callable

from app.services.invoice_golden_evaluation_service import build_golden_evaluation_report


def run_live_golden_evaluation(
    expected_cases: list[dict[str, Any]],
    runner: Callable[..., dict[str, Any]],
    *,
    provider_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    actual_cases: dict[str, dict[str, Any]] = {}
    actual_sheet_rows: dict[str, list[dict[str, Any]]] = {}
    for case in expected_cases:
        result = runner(case, create_google_sheet=False)
        actual_cases[case["id"]] = result.get("payload") or {}
        actual_cases[case["id"]]["source_files"] = result.get("source_files") or case.get("source_files") or []
        if result.get("sheet_rows") is not None:
            actual_sheet_rows[case["id"]] = result["sheet_rows"]
    return build_golden_evaluation_report(
        expected_cases,
        actual_cases,
        actual_sheet_rows=actual_sheet_rows,
        provider_config=provider_config,
    )
