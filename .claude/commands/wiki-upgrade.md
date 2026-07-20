Check for LLM-wiki updates and upgrade if available.

```bash
python3 scripts/version_check.py
```

If an update is available, ask the user if they want to upgrade, then run:

```bash
bash scripts/upgrade.sh
```

After upgrade, run `/wiki-check` to verify everything still passes.
