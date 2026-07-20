#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a compact golden-set evaluation report from replay fixtures.")
    parser.add_argument(
        "--cases",
        default="backend/tests/golden/invoice_photos.json",
        help="Path to the expected golden cases JSON.",
    )
    parser.add_argument(
        "--replay-dir",
        required=True,
        help="Directory with <case_id>.json replay fixtures.",
    )
    parser.add_argument(
        "--report-path",
        default="exports/golden_eval_report.json",
        help="Where to write the compact report JSON.",
    )
    parser.add_argument(
        "--provider-config",
        default="{}",
        help="JSON object with model/provider metadata to attach to the report.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "backend"))

    from app.services.invoice_golden_evaluation_service import build_golden_evaluation_report, load_golden_cases

    cases = load_golden_cases(args.cases)
    replay_dir = Path(args.replay_dir)
    actual_cases = {}
    actual_sheet_rows = {}
    for case in cases:
        fixture_path = replay_dir / f"{case['id']}.json"
        if not fixture_path.is_file():
            raise FileNotFoundError(f"Replay fixture missing: {fixture_path}")
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        actual_cases[case["id"]] = payload.get("actual_payload") or {}
        actual_cases[case["id"]]["source_files"] = payload.get("source_files") or case.get("source_files") or []
        if payload.get("actual_sheet_rows") is not None:
            actual_sheet_rows[case["id"]] = payload["actual_sheet_rows"]

    report = build_golden_evaluation_report(
        cases,
        actual_cases,
        actual_sheet_rows=actual_sheet_rows,
        provider_config=json.loads(args.provider_config),
    )
    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
