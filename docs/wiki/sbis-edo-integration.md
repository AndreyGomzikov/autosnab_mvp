---
title: SBIS EDO Integration
source: session
created: 2026-07-02
tags: [integration, sbis, edo, raw]
status: draft
---

# SBIS EDO Integration

## Goal

Add a read-only integration with Saby / SBIS EDO that pulls incoming supplier documents into the same document flow as invoice photos/PDFs uploaded through the bot.

## Source documents

- `inbox/ТЗ для разработчика_ интеграция СБИС ЭДО с выгрузкой документов в таблицу.md`
- `inbox/Вставленное изображение.png`
- `inbox/Вставленное изображение (2).png`

## Core requirements

- Read-only only: no signing, rejecting, or modifying documents inside SBIS.
- Pull new incoming documents for active client legal entities.
- Read the full document card: id, type, number, date, amount, supplier, recipient, status, attachments.
- Download all relevant attachments.
- Persist raw files and metadata in local storage or object storage.
- Write one row per document into the working table.
- Prevent duplicates on repeated sync runs.
- Log auth, read, download, duplicate, and save errors.

## Classification

On MVP, do not hard-filter only UPD / TORG-12 / invoice.
Pull all incoming documents that may relate to goods supply, then classify them on our side:

- `goods_document`
- `not_goods_document`
- `unknown`

## Suggested table columns

- upload date
- source
- SBIS document id
- document type
- number
- date
- amount
- supplier name
- supplier INN/KPP
- recipient name
- recipient INN/KPP
- processing status
- classification
- raw file link
- SBIS card link
- error

## Raw storage

Keep raw documents in a structure like:

```text
/sbis-edo/client_{client_id}/YYYY/MM/{sbis_document_id}/
  document.xml
  print_form.pdf
  signature.sig
  metadata.json
```

## Sync model

- Poll every 5-15 minutes
- Fetch active legal entities
- Request new incoming documents by events/changes, not full history
- Download card and attachments
- Save raw files
- Write row to the working table
- Record execution result

## Open implementation questions

- Which SBIS auth method will we use in production credentials?
- Where exactly should the working table live in this repo flow?
- Do we store raw files in local filesystem only, or also in object storage later?
- Do we need a separate DB table for SBIS sync history and dedupe keys?
- There is an additional process hint from screenshots: OCR/document ingestion should stay centralized, not split between isolated per-source flows.
- One screenshot references an existing Google Docs table named `АвтоСнаб Кафе Ромашка` and mentions `@AndrewGF1` as a contact for the OCR/doc flow.

## Feasibility analysis

Yes, this is realistic in the current project, but only if the EDO flow is implemented as a new source adapter that feeds a shared document core.

### What already helps

- `Receiving`, `ReceivingDocument`, and `ReceivingItem` already give you a common persistence model for incoming documents.
- `invoice_review_service.py` already acts as a central orchestration layer for OCR, parsing, Google Sheets export, and iiko preview/export.
- The project already treats raw files, parsed metadata, and generated table rows as separate layers, which is the right shape for a multi-source pipeline.

### What is missing

- There is no SBIS client, auth layer, sync scheduler, or sync history table yet.
- There is no explicit source registry for "bot upload" vs "SBIS EDO" vs future sources.
- There is no raw-file storage abstraction for attachments and metadata snapshots.
- There is no central dedupe engine keyed by source document identity + attachment identity + content hash.
- The current code path is still mostly invoice-centric; EDO should not be bolted directly into invoice-review handlers.

### Best architecture

Use a shared core with source-specific adapters:

- `source adapters`:
  - bot upload
  - SBIS EDO
- `document core`:
  - normalize header
  - store raw payloads
  - dedupe
  - classify
  - write table row
- `presentation/export`:
  - Google Sheets
  - future export targets

### Recommendation

Do not centralize everything into one giant handler. Centralize the *document model and pipeline*, but keep SBIS as its own adapter. That gives you one place for business rules and multiple ingestion sources without turning the code into a monolith.

### Practical verdict

- For an MVP, yes, you can build your own EDO module.
- For a production-grade version, you should first refactor the project toward a source-agnostic intake pipeline.
- If you skip that refactor and wire SBIS straight into the current invoice-review path, it will work short-term but become fragile quickly.

## Parallel development constraint

Another developer is building the PDF export path in parallel, so we must keep both tracks aligned.

### Rule

- Do not fork the business model into separate PDF and SBIS versions.
- Do not let each developer invent their own document schema.
- Agree on one canonical document contract first, then let each source implement an adapter to that contract.

### Practical boundary

- PDF developer owns the PDF source adapter and PDF-specific extraction.
- SBIS work owns the SBIS source adapter and SBIS-specific extraction.
- Shared code owns the canonical document model, dedupe, raw artifact storage, classification, and table writer.

### Coordination artifact

Create and maintain one short interface spec in the repo:

- canonical document header
- line item shape
- raw artifact shape
- required processing statuses
- table column mapping rules

If this spec changes, both tracks must update together. That is the main guardrail against divergence.

## Final execution plan

1. Freeze the canonical document contract with the PDF developer.
2. Keep the current PDF path unchanged and add SBIS as a separate source adapter.
3. Introduce a shared document core for normalization, dedupe, raw storage, and status tracking.
4. Make both PDF and SBIS write into the same working table through the same writer contract.
5. Validate the result on one PDF document and one SBIS document before expanding the scope.

### MVP boundary

- No separate repo.
- No rewrite of the current OCR flow.
- No duplicated business schema for PDF and SBIS.
- No direct SBIS coupling into invoice-review handlers.

## Delivery priority from latest screenshot

- This week: show a working MVP for document recognition and table placement.
- Next week: move to SBIS EDO work.
- Keep the scope visible and ask for help early if blocked.
- If the line stays stable, the work can continue in this direction on commercial terms.

## Meeting agenda intake

The meeting notes added on 2026-07-03 clarify the immediate task split:

- make multi-page document upload and page visibility comfortable for the user
- coordinate business-logic questions with Lilia
- share Lilia's contacts to keep communication moving
- review parsing options, including whether a service account is needed

### Practical implication

The short-term work is still the same MVP document path, but with a stronger emphasis on multi-page document UX and parsing strategy before SBIS becomes the next-week focus.

## Table structure and Apps Script constraints

The new notes and screenshot on 2026-07-03 clarify the working table behavior:

- the table is an intermediate validation layer between the первичный документ and the accounting system
- columns are grouped conceptually into:
  - fields from the original document
  - fields from the accounting system reference data
  - recalculation fields
  - validation / status fields
  - final fields for the accounting system
- the existing Apps Script looks up column names from the second row, so the current header names must not be renamed casually
- document-level upload/testing currently works through the `Загрузка` column
- if a document already has an error, duplicate, or line under review, it should not be uploaded
- after testing, the row status becomes `Загружено` or `Отправлено в УС`
- a future `Вернуть на проверку` action is planned for cases where a previously uploaded document needs to be corrected

### Source-specific implication

- keep the current header names stable
- avoid table schema drift between the PDF path and the SBIS path
- define only the canonical contract and the writer rules, not a new header set per source
