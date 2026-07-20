import json
from pathlib import Path

from app.services.invoice_golden_evaluation_service import (
    build_golden_evaluation_report,
    evaluate_golden_case,
    evaluate_golden_replay_fixture,
)
from app.services.invoice_golden_live_eval_service import run_live_golden_evaluation


GOLDEN_PATH = Path(__file__).parent / "golden" / "invoice_photos.json"
REPLAY_PATH = Path(__file__).parent / "golden" / "replays" / "upd-1928.json"


def test_real_photo_golden_set_has_five_pages_grouped_as_four_documents():
    cases = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    assert len(cases) == 4
    assert sum(len(case["source_files"]) for case in cases) == 5
    assert any(len(case["source_files"]) == 2 for case in cases)


def test_golden_evaluator_reports_exact_field_path():
    expected = {
        "id": "sample",
        "document": {"invoice_number": "42"},
        "expected_item_count": 1,
        "items": [{"quantity": 2}],
    }
    actual = {
        "invoice_number": "41",
        "items": [{"quantity": 3}],
    }

    report = evaluate_golden_case(expected, actual)

    assert report["passed"] is False
    assert [item["path"] for item in report["mismatches"]] == [
        "document.invoice_number",
        "items[0].quantity",
    ]


def test_golden_evaluator_normalizes_product_spacing_but_not_numbers():
    expected = {
        "id": "spacing",
        "items": [{"normalized_name_candidate": "Еноки вес", "quantity": 3.14}],
    }
    actual = {
        "items": [{"normalized_name_candidate": "Ен Оки Вес", "quantity": 3.14}],
    }

    assert evaluate_golden_case(expected, actual)["passed"] is True


def test_golden_evaluator_compares_expected_sheet_rows():
    expected = {
        "id": "sheet",
        "expected_sheet_rows": [
            {"№ Документа": "42", "Наименование товара в УС": "Еноки вес"},
        ],
    }
    actual = {}
    report = evaluate_golden_case(
        expected,
        actual,
        actual_sheet_rows=[{"№ Документа": "42", "Наименование товара в УС": "Ен Оки Вес"}],
    )

    assert report["passed"] is True


def test_golden_replay_fixture_evaluates_saved_payload():
    report = evaluate_golden_replay_fixture(REPLAY_PATH)

    assert report["case_id"] == "upd-1928"
    assert report["passed"] is True


def test_build_golden_evaluation_report_returns_compact_metrics():
    cases = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    actual_cases = {
        case["id"]: {
            **case["document"],
            "items": case["items"],
            "source_files": case["source_files"],
        }
        for case in cases
    }
    actual_sheet_rows = {
        case["id"]: case.get("expected_sheet_rows") or []
        for case in cases
    }

    report = build_golden_evaluation_report(
        cases,
        actual_cases,
        actual_sheet_rows=actual_sheet_rows,
        provider_config={"provider": "replay", "model": "gpt-5-mini"},
    )

    assert report["provider_config"]["provider"] == "replay"
    assert report["summary"]["cases_total"] == 4
    assert report["summary"]["cases_passed"] == 4
    assert report["summary"]["correct_page_grouping_rate"] == 1.0


def test_live_golden_evaluator_never_requests_google_sheet_write():
    cases = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    calls = []

    def fake_runner(case, *, create_google_sheet):
        calls.append((case["id"], create_google_sheet))
        return {
            "payload": {
                **case["document"],
                "items": case["items"],
            },
            "source_files": case["source_files"],
            "sheet_rows": case.get("expected_sheet_rows") or [],
        }

    report = run_live_golden_evaluation(
        cases,
        fake_runner,
        provider_config={"provider": "live", "model": "gpt-5-mini"},
    )

    assert all(flag is False for _, flag in calls)
    assert report["summary"]["cases_passed"] == 4
