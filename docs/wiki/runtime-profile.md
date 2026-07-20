---
title: Runtime Profile of Validation Scripts
source: session
created: 2026-07-02
tags: [meta, ci, validation]
status: current
---

# Runtime Profile

Each script under `scripts/` declares its runtime in a `# runtime: ...` header.
Use this matrix to decide what runs in CI vs. on a dev machine.

## ci-safe — runs without raw files

| Script | What it checks | Notes |
|--------|----------------|-------|
| `wiki_check.py` | Required files, frontmatter, broken links, log header format, index references | Pure structural, no external deps |
| `raw_manifest_check.py` | Manifest schema (via `raw_sources.meta.json`), columns, IDs, statuses | Skips raw-file existence when `PROJECT_RAW_ROOT` is unset |
| `untracked_raw_check.py` | Files in repo that look like raw assets but aren't in the manifest | Scans whatever is on disk |
| `wiki_size_report.py` | Token-budget bucket (green/yellow/red/purple), largest pages | Reads `docs/wiki/` only |
| `provenance_check.py --ci` | Every non-session page has a `source_hash` field | Skips actual hash comparison |

## dev-only — needs `PROJECT_RAW_ROOT` pointing at real raw files

| Script | What it does | Why dev-only |
|--------|--------------|--------------|
| `provenance_check.py` (no `--ci`) | Compares `source_hash` against the live raw file | Needs raw files mounted |
| `stale_report.py` | Reports wiki pages whose source has changed | Reads raw mtimes/hashes |
| `delta_compile.py --write-drafts` | Scaffolds recompilation drafts for stale/new raw | Writes draft stubs |
| `ingest_raw.py` | Walks raw root, hashes, dedupes, updates manifest | Mutates manifest |
| `init_raw_root.py` | Creates the raw directory layout | One-shot setup |
| `version_check.py` | Polls GitHub for new LLM-wiki releases | Network call |
| `export_memory_repo.py` | Mirrors wiki to a separate repo | Writes outside repo |

## CI workflow

`.github/workflows/wiki-lint.yml` runs only the ci-safe set. Adding a
dev-only check there will fail because raw files aren't checked in.

If you want CI to compare against real raw files, mount them into the
runner (artifact upload, S3 sync, …) and set `PROJECT_RAW_ROOT` in the
job env. Don't commit raw files to satisfy CI — that breaks the
compile-first contract.

## Local pre-commit recommendation

Before pushing, run the full local set:

```bash
python3 scripts/wiki_check.py
python3 scripts/raw_manifest_check.py
python3 scripts/untracked_raw_check.py
python3 scripts/wiki_size_report.py
python3 scripts/provenance_check.py
python3 scripts/stale_report.py
```
