---
title: Bot Backend API Contract
source: session
created: 2026-07-08
tags: [api, bot, backend, contract]
status: current
---

# Bot Backend API Contract

## Purpose

Freeze the first backend-facing contract for the document-upload bot without
duplicating the existing invoice-review business pipeline.

The bot remains an intake/status adapter only.

## Implemented endpoints

### `POST /api/v1/invoice-review/bot/upload-document-live`

Purpose:

- accept one logical document from the bot;
- create a durable upload-journal row;
- return `upload_id` plus `trace_id` immediately for async processing.

Current input shape:

- `files[]` - one or more files of one logical document
- `source_channel` - default `telegram_bot`
- `document_kind` - default `primary_document`
- `source_user_id` - required
- `source_username` - optional
- `source_chat_id` - optional
- `organization_name` - optional
- `point_name` - optional
- `create_google_sheet` - optional

Current behavior:

- images and PDF go into the existing invoice-review pipeline;
- unsupported-but-planned formats (`xml`, `xls`, `xlsx`, QR-specific flow) do
  not crash the endpoint and return `unsupported_format`;
- the durable journal is written before any long-running processing starts.
- the bot workflow may omit `extraction_method` completely; backend remains the
  only place that decides how the document will be parsed.

Immediate response fields:

- `upload_id`
- `trace_id` or `null`
- `status`
- `message`
- `source_channel`
- `document_kind`
- `files_count`
- `review_id` if already known
- `unsupported_reason` if applicable

### `GET /api/v1/invoice-review/bot/uploads/{upload_id}`

Purpose:

- return the durable current state of one bot upload;
- project raw trace/result data into short bot-facing statuses.

Current response fields:

- `upload_id`
- `trace_id`
- `status`
- `message`
- `completed`
- `source_channel`
- `document_kind`
- `files_count`
- `original_filename`
- `organization_name`
- `point_name`
- `user_id`
- `username`
- `review_id`
- `review_status`
- `result_code`
- `duplicate`
- `error_text`
- `uploaded_at`
- `updated_at`
- `google_spreadsheet_url`
- `google_spreadsheet_error`
- `document_summary`
- `pipeline_logs`
- `next_actions`

Current `document_summary` shape:

- `supplier`
- `invoice_number`
- `invoice_date`
- `document_form`
- `total_sum`
- `items_count`
- `pages_count`
- `duplicate_indicator`

## Durable upload journal

The new table `ingestion_uploads` is now the first persistent bot-ingestion
journal.

Persisted fields:

- `upload_id`
- `trace_id`
- `source_channel`
- `document_kind`
- `user_id`
- `username`
- `chat_id`
- `organization_name`
- `point_name`
- `original_filename`
- `file_type`
- `raw_file_path`
- `files_count`
- `status`
- `error_text`
- `review_id`
- timestamps

## Status contract

Current bot-facing statuses:

- `accepted_for_processing`
- `processing`
- `processed`
- `transferred_to_review`
- `requires_review`
- `processing_error`
- `unsupported_format`
- `possible_duplicate`

These statuses are intentionally bot-safe and do not expose internal backend
state names such as `ocr_processed` or `confirmed_full`.

## Current support boundary

Implemented now:

- `jpg`
- `jpeg`
- `png`
- `pdf`

Explicitly not implemented yet, but accounted for in the contract:

- `xml`
- `xls`
- `xlsx`
- QR-specific receipt flow

Those paths must return a business-readable unsupported response instead of a
technical failure.

## Integration rule

The bot contract is a thin adapter over `_process_invoice_upload(...)`.

That means:

- OCR/MinerU/OpenAI parsing stays in the existing backend;
- Google Sheets review write stays in the existing backend;
- duplicate detection stays in the existing backend;
- the bot stores provenance and projects statuses plus a short document summary,
  but does not own business review logic or parser selection.

## Draft/session endpoints (2026-07-09)

Added to support the cloud-`n8n` rebuild in
`docs/wiki/telegram-bot-cloud-n8n-plan.md`. These let `n8n` stay fully
stateless: all draft/session state lives in `ingestion_uploads` via a new
`collecting` status, keyed by `chat_id`. No new table was needed.

### `POST /bot/drafts/pages`

- Multipart form: `file`, `chat_id`, `source_user_id`, optional
  `source_username`, `document_kind`, `organization_name`, `point_name`.
- Creates the draft on the first call for a `chat_id`, or appends to the
  existing `collecting` row. Unsupported files are rejected without touching
  the draft. Enforces `settings.openai_max_image_pages` per draft and
  `settings.bot_upload_max_file_bytes` per file.
- Response: `upload_id`, `status` (`collecting` or `unsupported_format`),
  `pages_count`, `filenames`, `unsupported_reason`.

### `GET /bot/drafts/status?chat_id=...`

- Returns `{ "draft": null }` or the open draft's `upload_id`, `pages_count`,
  `filenames`, `organization_name`, `point_name`.

### `POST /bot/drafts/reset`

- Form field `chat_id`. Deletes the collecting row and its files. Safe no-op
  (`status: "no_active_draft"`) if nothing is open.

### `POST /bot/drafts/finalize`

- Form fields `chat_id`, optional `create_google_sheet`, `extraction_method`,
  `public_api_base_url`. 422 if there is no open draft or it has zero pages.
- Transitions the draft row to `accepted_for_processing`, assigns a
  `trace_id`, and starts the same background thread used by
  `/bot/upload-document-live`. Response shape is `BotUploadAcceptedResponse`,
  identical to the bulk endpoint.

### `GET /bot/uploads/latest?chat_id=...`

- Returns the most recent non-`collecting` upload for that `chat_id` through
  the same `BotUploadStatusResponse` shape as `/bot/uploads/{upload_id}`. 404
  if the chat has no history yet. This is what lets a bot `Статус` command
  work without ever storing an `upload_id`.

### Auth

All `/bot/*` endpoints (including the two pre-existing ones) now require
header `X-Bot-Api-Key` to equal `settings.bot_api_shared_secret` when that
setting is non-empty. Empty (default) disables the check for local dev. This
closes the "no auth" gap before the backend is exposed through a public
`ngrok` tunnel.

## Remaining gaps before production bot launch

- decide whether `organization_name` or `point_name` is the canonical required
  pre-upload selector;
- add stable deep-link/UI URL for `review_id`;
- implement XML/Excel/QR adapters when those flows become active;
- consider per-operator permission scoping beyond the single shared secret.
