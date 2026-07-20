Run all LLM-wiki validation checks on this project.

Execute these commands in order and report results:

```bash
python3 scripts/wiki_check.py
python3 scripts/raw_manifest_check.py
python3 scripts/untracked_raw_check.py
python3 scripts/provenance_check.py
python3 scripts/stale_report.py
```

If any check fails, explain what's wrong and how to fix it.
If all pass, say "Wiki health: OK" with the summary counts.
