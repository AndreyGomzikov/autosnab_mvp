---
title: Bot And SBIS Implementation Plan
source: session
created: 2026-07-08
tags: [plan, integration, sbis, bot]
status: draft
---

# Bot And SBIS Implementation Plan

## Purpose

Convert the already working invoice-review backend into two additional entry adapters:

- bot upload
- SBIS / Saby EDO sync

The key rule is to reuse the existing document-processing core and not duplicate OCR, AI, mapping, review, or export logic inside either adapter.

## What already exists in code

- The central invoice-review backend already exists and is the canonical processing core.
- Multi-page upload, extraction/evidence collection, OpenAI normalization, Google Sheets review writing, iiko reference enrichment, and iiko XML preview/export are already implemented.
- The repo does not yet contain a dedicated Telegram bot implementation.
- The repo does not yet contain an SBIS adapter, auth client, scheduler, or sync-history persistence.

## Working assumption

The shortest safe path is:

1. treat bot and SBIS as external source adapters;
2. keep one backend document contract;
3. feed both sources into the same review/write pipeline;
4. keep iiko export behind the existing reviewed-document flow.

## Bot Plan

### Bot TOR now fixed from `ТЗ бота.pdf`

The reviewed PDF confirms the bot's business role more narrowly:

- bot is only an intake/status channel for primary documents;
- parsed results must end up in the existing data-review module;
- bot must not push data directly into the accounting system;
- bot must not perform manual product matching or final business validation.

The TOR also fixes the minimum intake surface:

- images: `jpg`, `jpeg`, `png`
- `pdf`
- `xml`
- `xls` / `xlsx`
- cashier-check QR input

If a format is not supported by the current backend path, the bot must return a
clear unsupported-format response instead of failing technically.

The TOR fixes minimum persisted upload metadata as well:

- upload id
- user id
- username
- upload timestamp
- original filename
- file type
- stored raw-file link/path
- selected organization / location
- processing status
- error text if present

The TOR also fixes operator-visible statuses that the adapter layer must expose
in business language:

- file received
- accepted for processing
- processing
- processed
- transferred to review module
- requires review
- processing error
- unsupported format
- possible duplicate

### Phase 1. Freeze the integration boundary

- Reuse the existing invoice-review backend as the only document-processing entrypoint.
- Define one explicit bot-facing contract:
  - upload one logical document with one or more pages;
  - receive `trace_id`, document/review id, current status, and operator-facing error text;
  - provide a stable way to fetch processing progress and final result.
- Do not let the bot set business statuses directly.

### Phase 2. Implement the bot as a thin client

- Accept supported files from the user: image, PDF, XML, Excel, or receipt QR.
- Group pages into one logical document before upload.
- Forward the payload to the existing backend upload flow.
- Poll trace/status until processing completes.
- Return a short outcome:
  - uploaded for review;
  - needs manual review;
  - failed before review;
  - already looks like duplicate.

### Phase 3. Add minimum operator actions

- Show current processing status for the latest uploaded document.
- Return a link or identifier that lets the operator continue work in the main review surface.
- If backend exposes a safe retry action, allow bot-triggered retry for failed pre-review processing only.
- If multiple organizations/points exist for one operator, require explicit organization/location selection before final upload.

### Phase 4. Explicit non-goals for the bot

- No OCR logic in bot code.
- No product mapping logic in bot code.
- No sheet-row shaping in bot code.
- No iiko export generation in bot code.
- No private bot-only document schema.

## SBIS Plan

### Phase 1. Freeze the adapter contract

- Treat SBIS as a read-only source adapter.
- Pull document metadata and attachments, then hand them to the existing backend core.
- Keep source identity separate from business review status.

Required adapter fields:

- source system = `sbis`
- external document id
- document type
- document number
- document date
- supplier / recipient identifiers if present
- attachment list
- raw metadata snapshot
- sync timestamp
- dedupe key

### Phase 2. Build the SBIS client

- Add auth/config wrapper for production credentials.
- Add list/read/download methods for incoming documents.
- Download all relevant attachments for each document.
- Save raw artifacts and metadata snapshot before parsing.

### Phase 3. Add dedupe and sync history

- Create a sync-history store keyed by SBIS document id plus attachment identity/content hash.
- Record:
  - first seen time;
  - last sync attempt;
  - last successful ingestion;
  - backend review id if created;
  - last error.
- Prevent repeated sync runs from creating duplicate review documents.

### Phase 4. Connect SBIS to the shared pipeline

- Convert downloaded SBIS artifacts into the same logical-document input used by upload flow.
- Submit the document to the existing backend pipeline.
- Persist mapping between SBIS source id and internal review/document id.
- Expose operator-visible sync result:
  - ingested;
  - skipped as duplicate;
  - downloaded but failed processing;
  - source error.

### Phase 5. Add scheduler and retry rules

- Poll on a fixed interval.
- Retry only temporary transport/auth errors.
- Do not retry documents already marked as successfully ingested unless the source content changed.
- Keep retry and dedupe policy inside the SBIS adapter layer, not in review logic.

## Coordination Boundaries

### With backend/review work

- Backend owns document statuses, review payload shape, trace contract, and Google Sheets writer.
- Bot/SBIS must call that contract and must not fork it.

### With OCR/AI work

- Bot/SBIS pass files and metadata only.
- OCR, evidence merge, OpenAI parsing, normalization, and ambiguity handling remain in the existing pipeline.

### With iiko/export work

- Bot/SBIS stop at successful ingestion into the review pipeline.
- Accounting export remains a downstream reviewed-document step.
- No source adapter should send directly to iiko.

### With parallel colleague work

- Before implementation, agree on one canonical input/output contract for external sources.
- Any new source metadata fields must be additive and must not change the canonical business payload without agreement.
- If a colleague changes review payload/status names, bot and SBIS adapters must be updated together.

## Recommended delivery order

1. Smoke-test the existing invoice-review backend as an external API, without new logic.
2. Freeze upload journal/storage fields so bot-originated uploads retain source/user/location provenance required by the TOR.
3. Reuse the same adapter pattern for SBIS ingestion.
4. Add SBIS sync history and scheduler after manual ingestion works.
5. Only then automate larger operational loops.

## First smoke tests

### Bot smoke test

- Upload a two-page logical document through the bot adapter.
- Confirm that backend returns trace/status.
- Confirm that one review document is created.
- Confirm that the operator can continue in the existing review surface.

### SBIS smoke test

- Pull one incoming SBIS document with attachments.
- Save raw files and metadata.
- Ingest it through the same backend path.
- Confirm that duplicate re-sync does not create a second review document.

## Practical conclusion

The backend core already appears mature enough that bot and SBIS should be implemented as adapter layers, not as new business pipelines. The main risk is not missing processing logic; it is accidental contract drift between your adapters and colleagues' review/export work.
