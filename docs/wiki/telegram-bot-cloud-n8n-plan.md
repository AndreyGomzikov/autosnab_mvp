---
title: Telegram Bot via Cloud n8n — Fresh Implementation Plan
source: session
created: 2026-07-09
tags: [plan, bot, n8n, telegram, docker, ngrok]
status: draft
---

# Telegram Bot via Cloud n8n — Fresh Implementation Plan

## Why this replaces the old plan

All previous `n8n` workflow JSON files were deleted on 2026-07-09 to restart the
bot from scratch (see `docs/wiki/log.md`). The old design stored page binaries
and session state inside an `n8n` Data Table (`telegram_bot_sessions`). That is
exactly the accidental complexity flagged in
`docs/wiki/backend-bot-integration-review.md`: one logical upload was split
across three overlapping state stores (`ingestion_uploads`, in-memory trace,
`n8n` session row).

This plan removes the middle store. `n8n` keeps **no business state at all** —
not even a session table. All draft/session state now lives in the existing
backend database (`ingestion_uploads`), keyed by Telegram `chat_id`. `n8n`
becomes a pure stateless router: receive Telegram update, forward to backend,
relay backend's answer back to Telegram.

`docs/wiki/n8n-bot-implementation-plan.md` and
`docs/wiki/bot-backend-api-contract.md` are still valid for UX wording,
supported formats, and the existing `/bot/upload-document-live` /
`/bot/uploads/{upload_id}` endpoints, which remain in place for the web upload
page and as the underlying processing step. This page defines the new
draft-session layer that replaces `n8n`-side session storage.

## Today's priority scope

1. Bot must accept scans of накладные — one page or several pages of the same
   document.
2. Bot UX must be simple and self-explanatory for a non-technical operator.
3. Bot must functionally replace the web upload page, not duplicate its logic.
4. All parsing/normalization/Google Sheets logic stays exactly where it is
   today, in the FastAPI backend.
5. Backend runs locally in Docker; the bot (cloud `n8n`) reaches it only
   through an `ngrok` tunnel.

SBIS integration is explicitly out of scope for today.

## Architecture decision: backend owns all session state

Reuse the existing `ingestion_uploads` table and `IngestionUpload` model
(`backend/app/models/ingestion.py`) instead of adding a new table. Add one new
status value, `collecting`, that represents an open, not-yet-finalized draft
for one `chat_id`:

- First page for a chat with no open draft: create a row with
  `status="collecting"`, store the page under a draft directory, `files_count=1`.
- Next pages for the same chat while `status="collecting"`: append the file to
  the same draft directory, increment `files_count`, keep the row unchanged.
- `Готово` (finalize): validate `files_count >= 1`, transition the row to
  `status="accepted_for_processing"`, assign `trace_id`, and start the exact
  same background processing thread the bulk endpoint already uses
  (`_process_bot_upload_background`). From this point the row behaves exactly
  like today's bulk-uploaded row.
- `Сбросить` (reset): delete the collecting row and its draft directory.
- Only one `collecting` row per `chat_id` is allowed at a time. A second file
  arriving after `Готово` was already pressed (draft finalized) auto-opens a
  new draft, matching the existing "first file auto-opens session" UX rule.

This needs no schema migration — only a new status string and a handful of new
service functions plus new endpoints. It removes the third state store
entirely: `n8n` never needs to remember `chat_id -> file list` because backend
already indexes `ingestion_uploads.chat_id`.

## New backend endpoints (extend `backend/app/routers/invoice_review.py`)

All new endpoints live under the existing `/bot` prefix next to
`upload-document-live` and reuse `bot_ingestion_service.py` helpers.

### `POST /bot/drafts/pages`

- Multipart form: one `file`, `chat_id` (required), `source_user_id`
  (required), `source_username`, `organization_name`, `point_name` (optional).
- Validates format via the existing `classify_bot_file(...)`; unsupported
  files return the same `unsupported_format` shape used today, without
  touching any draft.
- Creates the draft row on first call for a `chat_id`, or appends to the
  existing `collecting` row.
- Response: `{ upload_id, status: "collecting", pages_count, filenames }`.

### `GET /bot/drafts/status`

- Query param `chat_id`.
- Returns the open draft (`pages_count`, `filenames`, `organization_name`,
  `point_name`) or `{ "draft": null }` if there is none.

### `POST /bot/drafts/reset`

- Body/query `chat_id`.
- Deletes the collecting row and its files from
  `settings.uploaded_invoices_dir`. No-op (safe) if no draft is open.

### `POST /bot/drafts/finalize`

- Body/query `chat_id`, optional `create_google_sheet`, `extraction_method`.
- 422 if no open draft or `files_count == 0`.
- Transitions the row exactly like today's `upload-document-live` handler
  does after saving files: assigns `trace_id`, calls `initialize_trace`,
  `append_trace_log`, starts `_process_bot_upload_background` in a daemon
  thread.
- Response shape: identical to `BotUploadAcceptedResponse` (already used by
  the bulk endpoint), so `n8n` reuses the same downstream polling logic either
  way.

### `GET /bot/uploads/latest`

- Query param `chat_id`.
- Looks up the most recent non-`collecting` `ingestion_uploads` row for that
  `chat_id` and returns it through the existing `get_bot_upload_status(...)`
  response building (same `BotUploadStatusResponse` shape). This lets `Статус`
  work without `n8n` ever storing an `upload_id`.

### Implementation notes

- Refactor the shared "save file to draft dir + validate format" logic out of
  the current `upload-document-live` handler into a small helper reused by
  `POST /bot/drafts/pages`, so both flows share one path-safety and
  size-limit implementation (`settings.bot_upload_max_file_bytes`).
- Add an index on `IngestionUpload.chat_id` — it is currently unindexed and
  will now be queried on every bot interaction.
- Add focused tests in `backend/tests/test_bot_ingestion_service.py` and a
  router-level test: append two pages, finalize, confirm one logical
  document; reset before finalize; finalize with zero pages returns 422;
  second `chat_id` does not see the first chat's draft.

## Security: required before exposing via `ngrok`

The current bot endpoints have no auth. Once reachable through a public
`ngrok` URL, anyone who guesses/leaks the URL can upload files or read
statuses. Add a minimal shared-secret check before today's rollout:

- New setting `bot_api_shared_secret` (env-driven, empty disables the check
  for local dev).
- All `/bot/*` endpoints require header `X-Bot-Api-Key` to match when the
  secret is configured; mismatch returns 401.
- `n8n` HTTP Request nodes send this header via a stored credential
  (Header Auth), not a literal value pasted into node JSON.

## Deployment topology

```
Telegram  <-->  n8n (cloud)  <-- HTTPS -->  ngrok tunnel  <-->  Docker backend (local)
```

- Backend: `docker compose --profile public-tunnel up --build` starts both
  `backend` and `ngrok` services already defined in `docker-compose.yml`.
- `NGROK_AUTHTOKEN` must be set in `.env` for the tunnel to come up.
- Get the current public URL with `python3 scripts/get_ngrok_public_url.py`.
- Paste that URL into the `Workflow Config` node in `n8n` as `backendBaseUrl`.
- Free-tier `ngrok` URLs change on every restart. For today's MVP, accept
  manual re-paste after a backend restart. If backend restarts turn out to be
  frequent, the follow-up is a static/reserved `ngrok` domain
  (`ngrok http --domain=<reserved> 8000`, wired through a `NGROK_STATIC_DOMAIN`
  env var into the compose `ngrok` service command).
- Practical consequence to flag to the user: while the local Docker backend
  is down, the bot cannot process anything — the machine running Docker must
  stay on and connected during working hours.

## Cloud n8n workflow design (stateless)

One workflow, few nodes, no Data Table, no static data.

1. **Workflow Config** (`Set` node) — `backendBaseUrl`, nothing secret; the
   bot token lives in an `n8n` Telegram credential, the shared secret lives in
   an `n8n` Header Auth credential used by the `HTTP Request` nodes.
2. **Telegram Trigger** — webhook mode.
3. **Normalize Update** (`Code` node) — extract `chat_id`, `user_id`,
   `username`, and classify the update into one intent:
   `start`, `file` (photo or document), `done`, `status`, `reset`, `unknown`.
   For `file`, resolve the Telegram `file_id` to download (largest `photo`
   entry, or `document.file_id` when the message is a raw document).
4. **Switch** on intent.
5. **Branch `file`**: Telegram `Get File` → download binary → `HTTP Request`
   multipart `POST {{backendBaseUrl}}/bot/drafts/pages` with `chat_id`,
   `source_user_id`, `source_username`, the binary → reply
   `Страница {{pages_count}} добавлена. Пришлите ещё страницы или нажмите «Готово».`
   (or the unsupported-format message when backend returns that reason).
6. **Branch `done`**: `HTTP Request POST {{backendBaseUrl}}/bot/drafts/finalize`
   → reply `Принял, обрабатываю документ...` → bounded poll loop:
   `Wait 5s` → `HTTP Request GET {{backendBaseUrl}}/bot/uploads/{{upload_id}}`
   → `IF completed` → send final message (see UX below) else loop, capped at
   ~24 iterations (~2 minutes). If still not complete after the cap, send
   `Обработка продолжается дольше обычного. Наберите «Статус» через минуту.`
   and stop — the user can re-check any time because state lives in backend,
   not in this execution.
7. **Branch `status`**: `HTTP Request GET {{backendBaseUrl}}/bot/drafts/status`;
   if a draft is open, report page count; otherwise
   `GET {{backendBaseUrl}}/bot/uploads/latest` and report that result.
8. **Branch `reset`**: `HTTP Request POST {{backendBaseUrl}}/bot/drafts/reset`
   → reply `Черновик очищен.`
9. **Branch `start`/`unknown`**: reply with the short menu text (see UX).

Every `HTTP Request` node attaches the Header Auth credential from step 1.

## Telegram UX

Persistent reply keyboard: `Готово`, `Статус`, `Сбросить` (drop the earlier
`Новый документ` button — the first incoming file auto-opens a draft, so an
explicit "start" step is one less tap for the operator).

Reply texts (kept short, no automation-sounding wording):

- First file in a fresh draft: `Страница 1 добавлена. Пришлите ещё страницы накладной или нажмите «Готово», если это всё.`
- Next file: `Страница {{n}} добавлена.`
- Unsupported file: `Этот формат пока не поддерживается. Пришлите JPG, PNG или PDF.`
- `Готово` pressed with an open draft: `Принял, обрабатываю документ...`
- `Готово` pressed with nothing collected: `Сначала пришлите хотя бы одну страницу.`
- Final result, by `result_code` (reuse `derive_bot_result` wording already in
  `bot_ingestion_service.py`):
  - `transferred_to_review` → `Документ обработан и передан на проверку.` + document summary (supplier / invoice number / date / sum) + Google Sheet link if present.
  - `requires_review` → `Документ обработан, но нужна ручная проверка.` + summary + link.
  - `possible_duplicate` → `Похоже, этот документ уже был загружен ранее.`
  - `processing_error` / unsupported → plain failure text, no stack traces.
- `Статус` with an open draft: `Черновик: {{n}} стр. Пришлите ещё или нажмите «Готово».`
- `Статус` with no draft: last known result for `bot/uploads/latest`.
- `Сбросить`: `Черновик очищен.`
- `/start` or unrecognized text: one short instruction block explaining the
  three buttons and "просто пришлите фото или PDF накладной".

## Delivery checklist for today

1. Backend: add `collecting` status handling, four new endpoints, shared
   secret check, chat_id index, tests. Update
   `docs/wiki/bot-backend-api-contract.md` with the new endpoints once built.
2. Local ops: set `NGROK_AUTHTOKEN` and a strong `BOT_API_SHARED_SECRET` in
   `.env`, bring up `docker compose --profile public-tunnel up --build`,
   confirm `/health/runtime` responds through the `ngrok` URL.
3. Cloud `n8n`: new empty workflow, Telegram credential, Header Auth
   credential, the 9 nodes/branches above.
4. Smoke tests (see below), then hand the bot chat link to the operator.

## Smoke tests

1. Single photo → `Готово` → terminal result with document summary and sheet
   link.
2. Two photos (two pages of one накладная) → `Готово` → backend creates one
   logical document, not two.
3. Re-send the same document → `possible_duplicate` result.
4. Unsupported file (e.g. `.docx`) → clear rejection message, draft state
   unaffected.
5. `Статус` mid-collection (before `Готово`) → correct page count.
6. `Сбросить` mid-collection → next file starts a fresh draft, no leftover
   pages from the reset one.
7. Stop the Docker backend mid-processing, confirm `Статус` still returns the
   last durable state instead of an `n8n` node error.
8. Restart `ngrok`, update only the `Workflow Config` node, confirm the bot
   works again without touching any other node.

## Open follow-ups (not blocking today)

- Static `ngrok` domain if backend restarts prove disruptive.
- SBIS adapter, per `docs/wiki/bot-sbis-implementation-plan.md`, after the bot
  is stable.

## Confirmed ТЗ gaps (2026-07-09 audit)

Direct re-check of `ТЗ бота.pdf` against the current code (see
`docs/wiki/log.md`, entry "audit | ТЗ бота.pdf re-checked directly against
code") found the core contract sound but left four ТЗ requirements
unimplemented. These are the concrete backlog items for the bot track, in
rough priority order:

1. **XML / XLS / XLSX parsing.** ТЗ §1/§3 lists these as first-stage formats.
   `classify_bot_file(...)` in `bot_ingestion_service.py` already recognizes
   the extensions but returns `unsupported_format` with the message "Формат
   файла входит в ТЗ бота, но его backend-разбор еще не реализован." — no
   parser exists yet. Needs a dedicated XML parser and an Excel/XLSX parser
   feeding into the same evidence contract OCR/MinerU use today.
2. **Receipt QR-code scenario.** ТЗ §1/§3/§6 explicitly asks for a QR-first
   path for кассовые чеки, separate from photo OCR, because receipt print
   quality/format varies too much for reliable OCR. There is currently no QR
   decoding anywhere in the codebase — a receipt photo just goes through
   generic image OCR like any накладная. Needs: QR decode (e.g. `pyzbar` or
   similar) → receipt lookup via the decoded QR payload/API instead of vision
   parsing, then the same evidence-to-review pipeline as today.
3. **Organization/point selection.** ТЗ §4: "если в системе несколько
   организаций/точек, пользователь должен выбрать нужную организацию или
   точку". `organization_name`/`point_name` already exist as optional fields
   on `POST /bot/drafts/pages` and are persisted on `IngestionUpload`, but
   nothing in the n8n workflow ever asks the user to pick one — the fields are
   simply never populated. Needs a selection step (inline keyboard or a
   `/organization` command) before the first page is accepted, gated on
   whether the business actually has more than one organization/point today.
4. **Per-user upload authorization.** ТЗ §4: "пользователь имеет право
   загружать документы". The only auth today is the single shared
   `X-Bot-Api-Key` secret, which authenticates the n8n↔backend channel as a
   whole, not individual Telegram users — any Telegram user who reaches the
   bot can upload. Needs an explicit allow-list or role check keyed on
   `source_user_id`/`chat_id` before a draft is opened.

Items 1 and 3 were already listed in `docs/wiki/bot-backend-api-contract.md`
under "Remaining gaps before production bot launch" and "Current support
boundary" respectively; items 2 and 4 are newly made explicit here.
