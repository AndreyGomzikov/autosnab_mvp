from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def evaluate_golden_case(
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    actual_sheet_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    checked = 0

    for field, expected_value in (expected.get("document") or {}).items():
        checked += 1
        actual_value = actual.get(field)
        if not _values_equal(f"document.{field}", actual_value, expected_value):
            mismatches.append(
                {
                    "path": f"document.{field}",
                    "expected": expected_value,
                    "actual": actual_value,
                }
            )

    expected_items = expected.get("items") or []
    actual_items = actual.get("items") or []
    if "expected_item_count" in expected:
        checked += 1
        if len(actual_items) != expected["expected_item_count"]:
            mismatches.append(
                {
                    "path": "items.count",
                    "expected": expected["expected_item_count"],
                    "actual": len(actual_items),
                }
            )
    for index, expected_item in enumerate(expected_items):
        actual_item = actual_items[index] if index < len(actual_items) else {}
        for field, expected_value in expected_item.items():
            checked += 1
            actual_value = actual_item.get(field)
            if not _values_equal(f"items[{index}].{field}", actual_value, expected_value):
                mismatches.append(
                    {
                        "path": f"items[{index}].{field}",
                        "expected": expected_value,
                        "actual": actual_value,
                    }
                )

    expected_sheet_rows = expected.get("expected_sheet_rows") or []
    if expected_sheet_rows:
        actual_sheet_rows = actual_sheet_rows or []
        checked += 1
        if len(actual_sheet_rows) != len(expected_sheet_rows):
            mismatches.append(
                {
                    "path": "sheet_rows.count",
                    "expected": len(expected_sheet_rows),
                    "actual": len(actual_sheet_rows),
                }
            )
        for index, expected_row in enumerate(expected_sheet_rows):
            actual_row = actual_sheet_rows[index] if index < len(actual_sheet_rows) else {}
            for field, expected_value in expected_row.items():
                checked += 1
                actual_value = actual_row.get(field)
                if not _values_equal(f"sheet_rows[{index}].{field}", actual_value, expected_value):
                    mismatches.append(
                        {
                            "path": f"sheet_rows[{index}].{field}",
                            "expected": expected_value,
                            "actual": actual_value,
                        }
                    )

    return {
        "case_id": expected.get("id"),
        "passed": not mismatches,
        "checked_fields": checked,
        "mismatches": mismatches,
        "needs_human_label": expected.get("needs_human_label") or [],
    }


def build_golden_evaluation_report(
    expected_cases: list[dict[str, Any]],
    actual_cases: dict[str, dict[str, Any]],
    *,
    actual_sheet_rows: dict[str, list[dict[str, Any]]] | None = None,
    provider_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    actual_sheet_rows = actual_sheet_rows or {}
    reports = [
        evaluate_golden_case(
            case,
            actual_cases.get(case["id"], {}),
            actual_sheet_rows=actual_sheet_rows.get(case["id"]),
        )
        for case in expected_cases
    ]
    total_mismatches = sum(len(report["mismatches"]) for report in reports)
    passed = sum(1 for report in reports if report["passed"])
    checked_fields = sum(report["checked_fields"] for report in reports)
    exact_document_fields = _exact_document_field_rate(expected_cases, actual_cases)
    non_empty_normalized_name_rate = _non_empty_normalized_name_rate(expected_cases, actual_cases)
    correct_page_grouping = _page_grouping_rate(expected_cases, actual_cases)
    return {
        "provider_config": provider_config or {},
        "summary": {
            "cases_total": len(expected_cases),
            "cases_passed": passed,
            "checked_fields": checked_fields,
            "total_mismatches": total_mismatches,
            "exact_document_field_rate": exact_document_fields,
            "non_empty_normalized_name_rate": non_empty_normalized_name_rate,
            "correct_page_grouping_rate": correct_page_grouping,
        },
        "cases": reports,
    }


def evaluate_golden_replay_fixture(fixture_path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    expected = payload["expected"]
    actual = payload["actual_payload"]
    return evaluate_golden_case(
        expected,
        actual,
        actual_sheet_rows=payload.get("actual_sheet_rows") or [],
    )


def load_golden_cases(cases_path: str | Path) -> list[dict[str, Any]]:
    return json.loads(Path(cases_path).read_text(encoding="utf-8"))


def _exact_document_field_rate(
    expected_cases: list[dict[str, Any]],
    actual_cases: dict[str, dict[str, Any]],
) -> float:
    total = 0
    matched = 0
    for case in expected_cases:
        actual = actual_cases.get(case["id"], {})
        for field, expected_value in (case.get("document") or {}).items():
            total += 1
            if _values_equal(f"document.{field}", actual.get(field), expected_value):
                matched += 1
    return round(matched / total, 4) if total else 1.0


def _non_empty_normalized_name_rate(
    expected_cases: list[dict[str, Any]],
    actual_cases: dict[str, dict[str, Any]],
) -> float:
    total = 0
    non_empty = 0
    for case in expected_cases:
        actual_items = (actual_cases.get(case["id"], {}) or {}).get("items") or []
        for item in actual_items:
            total += 1
            if str(item.get("normalized_name_candidate") or item.get("us_product_name") or "").strip():
                non_empty += 1
    return round(non_empty / total, 4) if total else 1.0


def _page_grouping_rate(
    expected_cases: list[dict[str, Any]],
    actual_cases: dict[str, dict[str, Any]],
) -> float:
    total = len(expected_cases)
    matched = 0
    for case in expected_cases:
        actual_source_files = (actual_cases.get(case["id"], {}) or {}).get("source_files") or []
        if list(actual_source_files) == list(case.get("source_files") or []):
            matched += 1
    return round(matched / total, 4) if total else 1.0


def _values_equal(path: str, actual: Any, expected: Any) -> bool:
    field = path.rsplit(".", 1)[-1]
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(actual) - float(expected)) <= 0.000001
    if field in {"normalized_name_candidate", "clean_name", "name", "Наименование товара в УС", "Наименование товара из документа"}:
        return _compact_text(actual) == _compact_text(expected)
    if field in {"unit", "document_unit", "us_unit", "Ед.изм.", "Ед.изм. в УС"}:
        return str(actual or "").strip().lower() == str(expected or "").strip().lower()
    return actual == expected


def _compact_text(value: Any) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", "", str(value or "").lower())
