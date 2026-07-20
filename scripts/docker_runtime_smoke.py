#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test Docker runtime dependencies and optional evidence extraction.")
    parser.add_argument("--sample-image", help="Optional sample image/PDF to run through evidence collection.")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "backend"))

    from app.services.document_extraction_service import _collect_openai_evidence
    from app.services.provider_health_service import provider_health

    health = provider_health()
    print(json.dumps({"providers": health}, ensure_ascii=False, indent=2))
    if args.sample_image:
        evidence = _collect_openai_evidence(args.sample_image, Path(args.sample_image).name)
        has_content = bool((evidence.get("raw_text") or "").strip() or evidence.get("structured_document") or evidence.get("page_sources"))
        if not has_content:
            raise RuntimeError("Evidence collection produced an empty payload.")
        print(
            json.dumps(
                {
                    "sample_image": args.sample_image,
                    "logical_document_id": evidence.get("logical_document_id"),
                    "evidence_version": evidence.get("evidence_version"),
                    "pages": evidence.get("pages"),
                    "provider_attempts": evidence.get("provider_attempts"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
