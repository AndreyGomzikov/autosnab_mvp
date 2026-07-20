# n8n bot artifacts

- `telegram-bot-mvp.workflow.json` — importable cloud-n8n workflow. Stateless
  router only; all draft/session state lives in the backend
  (`ingestion_uploads`, `collecting` status).
- `telegram-bot-node-setup.md` — import steps, credentials to create, and
  points to double-check after import.

Full design and rationale: `docs/wiki/telegram-bot-cloud-n8n-plan.md`.
Backend endpoint contract: `docs/wiki/bot-backend-api-contract.md`.
