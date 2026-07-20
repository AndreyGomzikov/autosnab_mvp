---
title: n8n Bot Implementation Plan
source: session
created: 2026-07-08
tags: [plan, bot, n8n, telegram]
status: superseded
---

# n8n Bot Implementation Plan

> Superseded on 2026-07-09 by `docs/wiki/telegram-bot-cloud-n8n-plan.md`. All
> previous `n8n` workflow JSON artifacts were deleted and the bot is being
> rebuilt with backend-owned draft/session state instead of an `n8n` Data
> Table. The UX wording, supported-format list, and error-handling checklist
> below are still valid reference material; the session-storage design
> (section "Recommended n8n Workflows" #2) is not.

## Purpose

Implement the first operator-facing bot through `n8n` without duplicating the
existing invoice-review business pipeline.

Target shape:

- Telegram is the user entrypoint.
- `n8n` is the orchestration and session layer.
- `autosnab_mvp` backend remains the only document-processing core.

## Architectural Rule

The bot must not contain its own:

- OCR logic
- parser logic
- product mapping logic
- Google Sheets row shaping logic
- iiko export logic

`n8n` should only collect files, group pages, call backend APIs, poll status,
and return short operator-facing outcomes.

The reviewed `ТЗ бота.pdf` fixes the functional boundary too:

- bot is an intake and notification channel only;
- parsed results must go into the existing review module;
- bot must not send data directly to the accounting system;
- unsupported formats must produce a clear user message, not a technical crash.

## Required Backend Contract

Before bot assembly, freeze three explicit backend capabilities.

### 1. Upload logical document

Input:

- one logical document
- one or more page files
- source = `telegram_bot`
- optional operator/source metadata
- selected organization / point if that choice is required for the operator

Expected output:

- `trace_id`
- internal logical document id if available
- internal review/document id if already created
- initial processing status
- operator-safe error text if upload was rejected early

### 2. Poll processing status

Input:

- `trace_id`

Expected output:

- current stage
- current status
- progress/log lines suitable for bot display
- final review/document id when available
- final failure reason when available

### 3. Fetch final operator result

Expected minimal result model:

- `processing`
- `uploaded_for_review`
- `needs_manual_review`
- `duplicate`
- `failed_before_review`

Optional but useful:

- review UI URL
- human-readable short summary
- duplicate reference id

## Bot UX Contract

The first release should support one simple operator scenario:

1. user starts a new document session;
2. user sends one or more images/PDF pages;
3. user presses `Готово`;
4. `n8n` uploads the logical document to backend;
5. `n8n` polls until a terminal state;
6. bot returns short result and review reference.

The bot should not try to infer document boundaries automatically in v1.

Supported intake types from the TOR:

- `jpg` / `jpeg` / `png`
- `pdf`
- `xml`
- `xls` / `xlsx`
- QR-based cashier-check input

Important implementation note:

- if the backend cannot process one of these formats yet, the bot must still
  accept the upload attempt safely and return a plain unsupported-format
  response instead of breaking the session flow

## Recommended n8n Workflows

### 1. Telegram Incoming Router

Responsibility:

- receive webhook updates from Telegram;
- distinguish command/message/document/photo callbacks;
- route each update into the correct workflow branch.

Main branches:

- `/start`
- `Новый документ`
- file/photo received
- `Готово`
- `Статус`
- `Сбросить`

### 2. Collect Document Session

Responsibility:

- open or continue a document-upload session for a specific chat/user;
- append incoming files to the current logical document;
- preserve file order and Telegram metadata.

Persist per session:

- `chat_id`
- `user_id`
- `session_id`
- session status
- collected file list
- file order
- started_at / updated_at
- selected organization / point if applicable

Recommended first storage:

- `n8n` Data Store for MVP

Recommended production storage:

- external Postgres if concurrent operators or auditability become important

### 3. Finalize And Upload

Triggered by:

- `Готово`

Responsibility:

- validate that the session has files;
- download Telegram file binaries;
- convert them into backend multipart upload payload;
- send one request to the backend as one logical document;
- save returned `trace_id` and review identifiers.

Upload metadata to include:

- `source = telegram_bot`
- `telegram_chat_id`
- `telegram_user_id`
- `telegram_session_id`
- original filenames
- page order
- bot upload id / external upload reference

### 4. Poll Backend Status

Responsibility:

- poll backend by `trace_id` on a fixed interval;
- stop on terminal state;
- persist last known state;
- hand final state to the notification workflow.

Polling interval for MVP:

- every 5-10 seconds

Terminal states:

- `uploaded_for_review`
- `needs_manual_review`
- `duplicate`
- `failed_before_review`

### 5. Notify User Result

Responsibility:

- send a short Telegram message when processing finishes;
- include review id or UI link if available;
- keep messages operational, not verbose.

Suggested result texts:

- `Документ принят и отправлен на проверку`
- `Документ загружен, нужна ручная проверка`
- `Похоже на дубликат`
- `Обработка остановилась до проверки`
- `Файл получен, но этот формат пока не поддерживается`

### 6. Manual Support Commands

Minimum commands:

- `Статус` -> show last known status for active/latest session
- `Новый документ` -> open a fresh session
- `Сбросить` -> abandon unfinished current session

Optional later command:

- `Повторить` only for safe backend pre-review retry

## Recommended n8n Nodes

Minimal node set:

- `Telegram Trigger`
- `Switch`
- `Set`
- `Code`
- `Data Store`
- `HTTP Request`
- `Split In Batches` if file downloads need iteration
- `Wait` or scheduled re-entry for polling

Likely custom code responsibilities inside `Code` nodes:

- normalize Telegram update shape
- maintain session state object
- build multipart upload metadata
- map backend terminal statuses into short bot texts

## Session Protocol

Recommended explicit protocol for v1:

- command `Новый документ` opens a session
- each incoming photo/document is appended to the open session
- command `Готово` finalizes upload
- command `Сбросить` clears the unfinished session

This is safer than trying to auto-detect the end of a document from message
timing alone.

## File Grouping Rules

v1 grouping rules:

- all files received in the active session belong to one logical document
- preserve arrival order unless the user later gets a reorder UI
- reject a second active upload while the current one is still collecting files,
  unless the user explicitly resets

Important boundary:

- multi-file upload here means multiple pages of one document
- it must not silently become batch import of unrelated invoices

## Error Handling

The bot layer must surface these failure classes clearly:

- no files in session when `Готово` is pressed
- file is empty
- file exceeds size limit
- user has no permission to upload
- Telegram file download failure
- backend upload rejection
- backend timeout / unreachable backend
- backend terminal failure before review creation
- duplicate detection
- unsupported format

For each failure, save:

- timestamp
- session id
- trace id if already assigned
- short operator-facing error
- raw technical error for internal logs

The TOR also requires an upload journal beyond transient bot-session state. The
implementation therefore needs a durable record with at least:

- upload id
- user id
- username
- upload timestamp
- original filename
- detected file type
- stored raw-file link/path
- selected organization / point
- current processing status
- error text if present

## Security And Operations

Minimum operational rules:

- keep Telegram token and backend credentials in `n8n` credentials/secrets
- use webhook mode, not long polling, for production
- log workflow failures separately from user-facing messages
- add alerting for repeated backend unavailability
- do not expose internal backend traces verbatim to end users if they include
  technical stack details

## Delivery Sequence

### Iteration 1. Happy path MVP

- freeze backend upload/status contract
- build Telegram intake
- collect one logical document
- upload to backend
- poll final state
- send final result message

### Iteration 2. Session hardening

- add `Статус` / `Сбросить`
- persist active sessions
- cover multi-page document flow explicitly
- improve duplicate/failure messaging

### Iteration 3. Production readiness

- move persistent session state to durable storage if needed
- add alerts/log review
- add review UI deep-link
- add safe retry if backend supports it cleanly

## First Smoke Tests

### Smoke 1. Single-page photo

- user opens session
- sends one invoice photo
- presses `Готово`
- bot returns final review outcome

### Smoke 2. Two-page document

- user opens session
- sends two pages
- presses `Готово`
- backend creates one logical document, not two

### Smoke 3. Duplicate

- user re-sends the same document
- bot returns duplicate-like result instead of pretending a fresh review was created

### Smoke 4. Backend failure

- backend returns a pre-review error
- bot shows failure text and keeps the operator path clear

## Open Questions Before Implementation

- Which exact endpoint returns the best polling/status contract for `trace_id`?
- Is there already a stable review UI URL pattern for deep-linking from the bot?
- Does backend support an explicit `source = telegram_bot` metadata field today?
- Is safe retry available, or should retry stay manual in the web UI for now?
- Where should the durable upload journal live: current backend DB tables or a dedicated bot-ingestion table?
- What is the first-stage size limit per file/document?
- Will Telegram be the first channel, or does the same contract need to be usable for a future web-bot channel too?

## Practical Conclusion

The shortest path is not "build a smart bot"; it is "build a disciplined `n8n`
adapter over the existing backend". If the backend contract is frozen first, the
bot can be delivered incrementally without risking a second business pipeline.
