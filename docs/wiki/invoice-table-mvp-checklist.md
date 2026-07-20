---
title: Invoice Table MVP Checklist
source: session
compiled_from: [src_ffd34f0803, src_137b730e1a, src_4c95377e5e, src_121c42f2ea]
created: 2026-07-03
updated: 2026-07-04
tags: [mvp, checklist, invoices, table, implementation]
status: current
---

# Invoice Table MVP Checklist

## MVP goal

Build the shortest reliable flow from downloaded invoice documents to the working validation table.

Target flow:

```text
downloaded invoice
-> file intake
-> document extraction
-> document + line parsing
-> validation table row set
-> user review
-> gated upload action
```

## Do first

### 1. Freeze the target table contract

- take the real `АвтоСнаб Кафе Ромашка` sheet as the source of truth
- lock the effective machine-bound header row
- define the minimum MVP columns you must write
- separately mark:
  - source document fields
  - manual operator fields
  - computed/export fields

Without this, backend work will drift immediately.

### 2. Define a document-level status model

Minimum states for MVP:

- `ocr_failed`
- `recognized_needs_review`
- `possible_duplicate`
- `blocked_duplicate`
- `ready_for_upload`
- `uploaded`
- `returned_for_review`

The status decision must be document-level, even if rows are stored line-by-line.

### 3. Build a dedicated table writer

Do not rely on ad hoc row shaping inside old invoice-review logic.

Create one explicit mapping layer:

- normalized document -> table header fields
- normalized item -> table line fields
- computed fields -> final target columns

This is the safest reuse boundary if existing ingestion code stays.

### 4. Keep OCR failure non-fatal

Even when OCR/parser quality is poor:

- create the document shell
- create a reviewable table entry
- set blocking status
- let the operator fix manually

### 5. Add duplicate gating before upload

At MVP level, use simple document identity heuristics:

- document number
- document date
- supplier
- amount, if available

You do not need perfect dedupe first. You do need a hard stop for obvious duplicates.

## Reuse from current code

Reuse:

- upload endpoint
- file persistence
- Google Sheets shared-sheet writer and block insertion
- base DB persistence for review entities
- current extraction orchestration in `document_extraction_service.py`

Likely rewrite or isolate:

- parser backend selection
- normalized extraction schema
- sheet row builder
- target headers
- status mapping
- duplicate logic
- manual/computed/export field separation

## OpenAI-first parsing track

The new team decision should change one thing only: parsing authority.

- OpenAI should become the primary parser that decides document header fields, line items, document form, duplicate hints, and parser warnings
- OCR/MinerU should become evidence suppliers: raw text, markdown, page fragments, and optional image input for the OpenAI parser
- Google Sheets writing must stay deterministic; do not let the model write directly into the sheet contract

### Proposed implementation order

1. Freeze one normalized schema for the model output.
   Include document-level fields, item rows, confidence/warning flags, and explicit status hints that can later map into `Статус загрузки`, `Статус строки`, `Дубль`, and `Корректировка`.

2. Add a dedicated OpenAI parser service instead of extending regex rules further.
   Suggested boundary: new module such as `backend/app/services/openai_invoice_parser_service.py` that accepts raw document evidence and returns strict JSON.

3. Extend `document_extraction_service.py` with an `openai` backend mode.
   Keep `google_ocr`, `mineru`, and `hybrid` as evidence collectors or fallback paths, but make `openai` the primary field extractor when the token is configured.

4. Keep the final table mapper deterministic.
   `invoice_review_service.py` and `google_sheets_service.py` should consume a normalized payload and map it into the `Накладная` contract without model-side column decisions.

5. Add a validation/repair layer between model output and sheet writing.
   Validate dates, numeric fields, INN length, totals, status enums, and first-row-only document fields before creating the Google row block.

6. Build a small golden set before rollout.
   Use the known workbook examples plus a handful of real uploads to compare:
   `document -> OpenAI payload -> normalized payload -> sheet rows`
   The acceptance target is not "beautiful JSON"; it is exact row placement under the real `Накладная` headers.

### Suggested transport contract for the model

Minimum output groups:

- `document`: document form, number, date, supplier, supplier INN, shipper, recipient, trade point, warehouse, basis, total sum
- `items[]`: source item name, matched name hint, document unit, accounting unit hint, quantity, normalized quantity hint, price, VAT, line totals
- `review_flags`: OCR uncertainty, missing reference match, duplicate suspicion, document incompleteness
- `source_trace`: page number, quote/snippet, or span reference for each important extracted field

The `source_trace` block is important because the operator workflow is review-first. If the model says `ИНН Поставщика = X`, the system should later be able to show where that came from.

### Acceptance gates for the first implementation

- the inserted Google block preserves first-row-only document statuses
- line rows never shift relative to the real row-2 headers
- duplicates map to `Да` or `?` only through deterministic post-processing
- OCR failures still create a reviewable shell
- special forms such as receipts or purchase acts can leave supplier fields blank without breaking the pipeline
- sheet formulas and validations continue to work after insertion

### Implemented item-normalization boundary

The OpenAI item contract now carries `raw_name`, `clean_name`,
`normalized_name_candidate`, `brand_or_descriptor`, structured `package`,
document quantity/unit, conversion candidates, codes, confidence, and a
row-specific review reason.

`item_normalization_service.py` remains authoritative after model output:

- it removes technical prefixes/codes and re-extracts package data
- it recomputes conversion coefficients and accounting quantity
- it matches products against the fixed `Товары` headers
- it matches package variants against the fixed `Справочник фасовок` headers
- it emits `Нет в справочнике` or `Сопоставление` on the affected item only

The shared-sheet mapper consumes only these deterministic results for
`Наименование товара в УС`, `Ед.изм. в УС`, and `Кол-во в УС`.

## Backend implementation order

### Step 1

Introduce a new target contract module for the validation table:

- header constants
- allowed statuses
- field groups

### Step 2

Introduce a new builder/service:

- `document_to_validation_table(...)`
- `item_to_validation_row(...)`
- `compute_export_fields(...)`

### Step 3

Make current upload flow write through that builder instead of directly through old register assumptions.

### Step 4

Store enough metadata to support:

- duplicate checks
- manual recheck
- future `Вернуть на проверку`

### Step 5

Only after table writing is stable, refine parser quality and extra business rules.

## Explicitly postpone

Do not pull these into the first MVP unless they block table creation:

- full SBIS EDO integration
- broad supplier-catalog work
- multimodal search
- perfect product matching
- advanced unit-conversion exception engine
- full accounting upload automation for all edge cases

## Must-not-miss risks

- treating row status as primary and forgetting document-level gating
- coding against renamed headers instead of the real sheet contract
- mixing OCR output with user-entered mapping fields in one opaque layer
- assuming every document has supplier + INN
- letting duplicate documents upload because individual rows looked valid
- making conversion coefficients manual-only

## Practical 2-3 day cut

If speed matters, the smallest sensible cut is:

1. freeze the real target columns from `Накладная`
2. define the strict OpenAI output schema
3. add an `openai` extraction backend that returns normalized document + item payloads
4. map that payload into the existing shared-sheet writer
5. add deterministic status/duplicate post-processing
6. verify against the real Google sheet that values land exactly under the intended headers

That is enough for a real MVP. Everything else can follow after the table flow is trustworthy.
