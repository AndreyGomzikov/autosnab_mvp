#!/usr/bin/env python3
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.document_extraction_service import extract_invoice_document_set  # noqa: E402
from app.services.invoice_golden_evaluation_service import evaluate_golden_case  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate invoice photos without database or Google Sheets writes.")
    parser.add_argument(
        "--golden",
        type=Path,
        default=ROOT / "backend/tests/golden/invoice_photos.json",
    )
    parser.add_argument("--live", action="store_true", help="Call configured evidence providers and OpenAI.")
    parser.add_argument("--actual-dir", type=Path, help="Directory containing <case-id>.json replay payloads.")
    parser.add_argument("--case", action="append", dest="case_ids", help="Evaluate only the selected case ID.")
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "exports/golden/latest-report.json",
    )
    args = parser.parse_args()
    if not args.live and args.actual_dir is None:
        parser.error("Use --live or --actual-dir.")

    cases = json.loads(args.golden.read_text(encoding="utf-8"))
    if args.case_ids:
        requested = set(args.case_ids)
        cases = [case for case in cases if case["id"] in requested]
        missing = requested - {case["id"] for case in cases}
        if missing:
            parser.error(f"Unknown case IDs: {', '.join(sorted(missing))}")
    reports = []
    for case in cases:
        if args.live:
            paths = [str(ROOT / filename) for filename in case["source_files"]]
            result = extract_invoice_document_set(
                paths,
                case["source_files"],
                extraction_method="openai",
            )
            actual = result.get("payload") or {}
            if result.get("stop_recommended"):
                actual = {"items": [], "_pipeline_error": result.get("error")}
        else:
            actual_path = args.actual_dir / f"{case['id']}.json"
            actual = json.loads(actual_path.read_text(encoding="utf-8"))
        reports.append(evaluate_golden_case(case, actual))

    output = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live" if args.live else "replay",
        "passed": all(report["passed"] for report in reports),
        "cases": reports,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
