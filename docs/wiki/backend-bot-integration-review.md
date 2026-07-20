---
title: Backend Bot Integration Review
source: session
created: 2026-07-09
tags: [architecture, bot, backend, review]
status: current
---

# Backend Bot Integration Review

## Scope

Review the real integration boundary between the FastAPI backend and the current
Telegram bot MVP in `n8n`, with focus on why the structure now feels more
complex than the intended "thin bot adapter" shape.

## Main findings

### 1. The bot contract is only partially durable today

`ingestion_uploads` is durable, but the actual processing lifecycle still
depends on two non-durable mechanisms:

- a daemon `Thread` started directly from the HTTP handler;
- an in-memory trace store with 2-hour TTL.

This means a backend restart between `accepted_for_processing` and the final
journal update can leave a bot upload stuck in `processing`, while trace logs,
`result_code`, `next_actions`, and Google Sheet links disappear from RAM even
though the journal row still exists.

Code points:

- `backend/app/routers/invoice_review.py`
- `backend/app/services/upload_trace_service.py`

### 2. One logical bot upload is split across three state stores

Current state is distributed across:

- backend journal: `ingestion_uploads`
- backend trace: in-memory upload trace
- `n8n` session row: `telegram_bot_sessions`

That is the main reason the flow feels heavy. The same upload is represented
simultaneously as:

- backend upload status (`accepted_for_processing`, `processing`, ...)
- trace completion/result
- `n8n` draft/session status (`collecting`, `processing`, final backend status)

Each layer stores overlapping fields and makes independent transition decisions.
This creates drift risk whenever statuses, retry rules, or result payloads
change.

### 3. `n8n` stores full file binaries in its own session table

The current workflow keeps each page as base64 in `telegram_bot_sessions.files_json`
until the operator presses `Продолжить`, and only then re-uploads those same
files to backend.

That duplicates raw storage and makes the orchestration layer responsible for
heavy payload persistence. It is workable for MVP, but structurally it is the
opposite of a thin bot adapter. It also makes session rows grow with every page
and every photo/PDF size increase.

Code points:

- `n8n/telegram-bot-mvp.workflow.json`
- `backend/app/routers/invoice_review.py`

### 4. Status/result projection is duplicated and already inconsistent

Bot-facing result mapping is currently split between:

- `derive_bot_result(...)`
- `_bot_status_message(...)`
- `n8n` reply formatting in `Normalize Poll Response`

There is already one concrete inconsistency: `get_bot_upload_status(...)`
calculates the top-level `result_code` with `receiving`, but
`build_bot_next_actions(...)` recalculates `result_code` without `receiving`.
So duplicate detection based on stored review metadata can diverge between
`result_code` and `next_actions.result_code`.

## Simplification direction

The clean target shape is:

1. Backend owns the full upload state machine and durable progress.
2. Bot stores only chat/session routing state, or ideally only a backend draft
   ID.
3. `n8n` stops persisting file binaries and stops interpreting business result
   states beyond "still processing" vs "final payload received".
4. Upload orchestration for web UI and bot is extracted from
   `invoice_review.py` into one shared application service with two thin
   adapters on top.

## Practical next step

The highest-value refactor is not "rewrite the bot". It is to move draft file
assembly plus upload lifecycle into backend first:

- add a backend draft-upload API for bot page append/finalize;
- persist trace/events in DB, not RAM;
- reduce `n8n` to Telegram transport plus button routing.

That change removes most of the accidental complexity without touching the core
OCR/OpenAI/Google Sheets pipeline.
