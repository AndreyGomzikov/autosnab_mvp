---
title: Invoice Recognition Hardening Plan
source: session
compiled_from:
  - src_af981ad353
  - src_5c7f7149b6
  - src_c408edb166
  - src_6f6622e548
  - src_ec8c54a020
created: 2026-07-04
updated: 2026-07-04
tags: [invoices, openai, ocr, mineru, multipage, testing, plan]
status: current
---

# Invoice Recognition Hardening Plan

## Goal

Make the real-photo invoice path reliable:

```text
one logical document with one or more files/pages
-> safe image/PDF preparation
-> text and layout evidence
-> OpenAI structured parser with source images when available
-> deterministic validation and normalization
-> deterministic Google Sheets mapper/writer
```

The OpenAI model extracts candidates only. It does not choose sheet columns,
assign business statuses directly, match catalogs authoritatively, or write to
Google Sheets.

## Current blockers

1. **(Fixed 2026-07-09) Docker MinerU model cache.** Originally blocked
   because OpenCV could not load `libxcb.so.1` (fixed earlier by installing
   the missing system libs). Superseded by a second finding the same day: the
   live container's MinerU model cache was missing
   `models/TabRec/UnetStructure/unet.onnx` entirely, so MinerU was skipped on
   every single request via the existing health guard — not a rare failure,
   a permanently disabled provider, leaving Google Drive OCR as the *only*
   evidence provider in production. Repaired via
   `python3 -m mineru.cli.models_download -s huggingface -m pipeline` against
   the persistent `autosnab_hf_cache` volume; hit and fixed a second-order
   issue where an interrupted first download attempt left
   `models/MFR/unimernet_hf_small_2503` corrupted (config files present,
   weights missing) and the downloader's directory-exists check silently
   skipped re-fetching it — `mineru_health()` reported `ready: true` while a
   real document still failed. Deleting that one model directory and
   re-running the downloader fixed it for real, confirmed by an actual
   extraction call returning 3466 characters of structured text. MinerU is
   now a genuine fallback provider again, not just formally "ready" — see
   `docs/wiki/log.md` entry "fix | MinerU model cache repaired, real fallback
   provider restored" for the full trace. `mineru_health()`'s single-file
   check is still shallow relative to the real multi-model surface and could
   mask a future partial-cache corruption the same way; not hardened yet.
2. Google Drive OCR can fail with a TLS handshake timeout and currently leaves
   OpenAI with empty evidence.
3. **(Fixed 2026-07-09) Google Drive OCR export race condition.** Root-caused
   the user's report of identical repeated uploads producing different parsed
   results: `recognize_invoice_with_google_drive_ocr` called `files().export()`
   immediately after `files().create(..., ocrLanguage=...)`, but Drive's OCR
   conversion is asynchronous — the export can return a document that is still
   just a UTF-8 BOM, before the OCR text has actually been written server-side.
   Confirmed directly from production debug logs (`exports/openai_debug/`):
   the same file `file_110.jpg` uploaded three times in a row produced
   `raw_text` of length 1 (BOM only), length 1 again, then length 2351 (real
   text) on the third try — with all three provider attempts logged as
   `status: "success"`, because `"﻿".strip()` is truthy in Python, so the
   near-empty result was never flagged as a failure. Fixed in
   `backend/app/services/ocr_service.py`: `_export_ocr_text_with_retry` now
   polls `export()` (`GOOGLE_DRIVE_OCR_EXPORT_RETRY_ATTEMPTS=4`,
   `GOOGLE_DRIVE_OCR_EXPORT_RETRY_DELAY_SECONDS=2.0` between attempts) until
   the decoded text is at least `GOOGLE_DRIVE_OCR_MIN_TEXT_LENGTH=20`
   characters after stripping the BOM, and the BOM is now stripped from the
   text returned to the rest of the pipeline either way. Covered by
   `backend/tests/test_ocr_provider.py::test_export_ocr_text_retries_past_empty_bom_only_export`
   (replays the exact empty/empty/real sequence from the production logs) and
   `..._gives_up_after_exhausting_retries`. Not yet redeployed to the running
   container — needs a rebuild/restart of `autosnab_backend_mvp4` to take
   effect, then a re-test with the same repeated-upload scenario.
3. The `openai` path sends only OCR/MinerU text and structure, not the source
   image, although the configured model supports image input and Structured
   Outputs.
4. Upload accepts one file, so a multi-page invoice is split into unrelated
   documents.
5. Rotated and perspective-distorted photos are not normalized before
   extraction.
6. There is no executable golden-set comparison for the five reviewed photos.

## Delivery order

### Phase 0: freeze observable contracts

- Keep the existing Pydantic parser result as the model-output boundary.
- Add a versioned evidence contract containing:
  - logical document ID;
  - ordered pages;
  - original file metadata;
  - prepared image paths;
  - OCR/MinerU text and structured blocks;
  - extraction errors per provider;
  - provider timing and attempt count.
- Add a version field to debug traces and normalized payloads.
- Ensure every provider attempt is visible in live logs.
- Treat empty evidence, empty model JSON, invalid schema, and zero item rows as
  explicit stop conditions unless the document type legitimately has no items.
- Never create a Google Sheet block after a stopped pipeline.

Acceptance:

- one trace explains which providers ran, what they returned, and why the
  pipeline continued or stopped;
- no failed provider is silently converted into a successful empty payload.

### Phase 1: repair the runtime evidence path

- Fix MinerU/OpenCV in Docker:
  - prefer a headless OpenCV-compatible dependency if MinerU supports it;
  - otherwise install the minimal required Debian runtime libraries, including
    the package providing `libxcb.so.1`;
  - add a container smoke test that imports `cv2` and starts the MinerU CLI.
- Diagnose Google TLS failures separately from parser failures:
  - verify DNS, CA certificates, OAuth refresh, and outbound connectivity;
  - add bounded retries with exponential backoff for transient handshake and
    timeout errors;
  - log the failing Google operation without exposing tokens;
  - return a typed provider error after retries are exhausted.
- Add health checks for `mineru`, `google_ocr`, and `openai` readiness.

Acceptance:

- a clean `docker compose up --build` passes the MinerU import/CLI smoke test;
- one sample image produces non-empty evidence in Docker;
- a Google OCR outage is reported as a provider outage, not as an empty invoice;
- OpenAI invocation count is visible in the trace.

### Phase 2: add logical multi-page upload

- Change the upload UI to accept multiple images and PDFs for one document.
- Show ordered page previews with reorder and remove controls.
- Add a logical upload/document ID distinct from individual page/file IDs.
- Persist page order and original filenames.
- Preserve the current single-file endpoint as a compatibility adapter.
- Pass all pages through one extraction and one OpenAI parse request.
- Reject or flag pages whose document number/supplier conflicts with page 1.

Acceptance:

- photos `...8411` and `...8412` are parsed as one invoice
  `УПМК003248`;
- document-level fields come from the complete page set;
- item rows from all pages are retained in source order;
- only one document block and one document ID are created.

### Phase 3: deterministic document preparation

- Preserve originals and generate separate prepared derivatives.
- Apply EXIF orientation and deterministic 90-degree rotation detection.
- Add deskew, perspective correction, border crop, contrast normalization, and
  conservative upscale for small text.
- Record every transformation in `source_trace`.
- Calculate basic quality signals: resolution, blur, clipping, glare, and
  estimated text coverage.
- Stop or require review when the image is too damaged for reliable extraction.

Acceptance:

- both rotated UPD photos enter OCR and vision upright;
- prepared derivatives remain traceable to their originals;
- transformations never overwrite uploaded source files.

### Phase 4: make OpenAI parsing genuinely multimodal

- Extend `openai_invoice_parser_service.py` to send:
  - OCR/MinerU text as textual evidence;
  - ordered prepared page images as image inputs;
  - page labels so `source_fragment` can identify the source page.
- For image documents, use text plus images together rather than waiting for
  OCR to become empty. Non-empty OCR may still contain wrong digits.
- Keep strict Pydantic Structured Outputs and the current deterministic
  normalization after the model.
- Add configurable limits for page count, image size, timeout, and retries.
- Keep `gpt-5-mini` as the baseline. Benchmark it against the selected newer
  mini candidate; change the default only if golden-set accuracy improves
  enough to justify latency and cost.
- Rename UI wording so “OpenAI parser” and “OpenAI vision parser” accurately
  describe whether images are sent.

Acceptance:

- debug evidence proves that OpenAI received each intended page;
- the model can recover a field from the image when OCR text omitted it;
- malformed or empty structured output stops before persistence/writing;
- the model still has no Google Sheets access or column-selection authority.

### Phase 5: strengthen deterministic validation

- Validate INN length and checksum and detect merged `ИНН/КПП`.
- Cross-check document number, date, supplier, totals, and VAT across pages.
- Recalculate each line and document totals using decimal arithmetic and
  configured tolerances.
- Detect missing continuation pages from page markers and inconsistent totals.
- Classify supported forms deterministically: UPD, TORG-12/товарная накладная,
  receipt, and unknown.
- Preserve repeated receipt lines exactly as source rows during parsing.
- If aggregation is required for accounting, perform it as a separate
  deterministic post-processing step with an audit link to original lines.
- Guarantee non-empty `Наименование товара в УС` from the normalized candidate
  even when catalog matching fails.
- Implement the deterministic conversion contract from
  `unit-conversion-rules.md`:
  - define the coefficient as accounting units per one document unit;
  - calculate both `Кол-во в УС` and `Цена в УС`;
  - use `Decimal` and verify that conversion preserves the line amount;
  - recompute standard coefficients from package value and units instead of
    trusting model output or manual user input;
  - add a deterministic product-exception reference for relations such as
    `шт -> кг`;
  - flag ambiguous or conflicting exceptions as `Сопоставление`;
  - retain conversion method, inputs, source reference, and unrounded results
    in debug metadata.

Acceptance:

- wrong or ambiguous INN cannot silently become `Распознано`;
- totals and VAT discrepancies produce item/document review flags;
- first-row-only document fields and row-specific corrections remain intact.
- `Цена в УС` is no longer left empty when conversion inputs are valid;
- `quantity_document * price_document` equals
  `quantity_us * price_us` within the configured tolerance;
- no ambiguous product exception is selected automatically.

### Phase 6: turn the photos into an executable golden set

- Store expected fixtures outside production code:
  - source document identity;
  - page grouping and order;
  - expected header fields;
  - expected source item rows;
- expected normalized names and units;
- expected conversion factor, method, accounting quantity, and accounting
  price;
- expected totals and review flags;
  - expected `Накладная` rows.
- Cover the four logical documents:
  - UPD `1928`;
  - UPD `УТ-35634`;
  - two-page invoice `УПМК003248`;
  - retail receipt with repeated kefir lines and one bag line.
- Add three test levels:
  - deterministic unit tests with mocked provider responses;
  - replay tests from saved evidence/model JSON;
  - opt-in live provider evaluation that never writes to Google Sheets.
- Produce a compact evaluation report per model/provider configuration.

Required metrics:

- exact match for document number, date, supplier INN, line quantities, prices,
  VAT, and totals;
- item row precision/recall;
- non-empty normalized product name rate;
- correct page grouping;
- correct review/stop decision;
- exact sheet-column mapping.

Release gate for this set:

- all four documents grouped correctly;
- 100% exact match on document number/date/INN and numeric line fields;
- 100% non-empty normalized product names;
- 100% correct deterministic conversions for rows with complete inputs;
- no sheet write from a stopped or schema-invalid pipeline.

### Phase 7: live Google Sheets retest

- Run the complete chain against the user-owned test sheet only after phases
  1–6 pass.
- Upload each logical document once.
- Compare written rows with the golden expected rows by row-2 header names.
- Verify formulas, validations, separator rows, and first-row-only fields.
- Verify duplicate behavior by uploading one confirmed duplicate.
- Record sheet ID, inserted range, trace ID, model, and evidence version in the
  test report.

Acceptance:

- values land under the exact existing headers;
- formulas and validations are unchanged;
- document statuses appear only in the first row;
- corrections appear only in affected item rows;
- document IDs are unique and stable.

## Suggested implementation batches

### Batch A: unblock and measure

- Phase 0 trace contract
- Phase 1 Docker/Google fixes
- golden fixtures and a no-write live evaluator

This batch must come first because model quality cannot be measured while no
evidence reaches OpenAI.

### Batch B: improve real-photo quality

- Phase 2 multi-page intake
- Phase 3 preparation
- Phase 4 multimodal OpenAI input

### Batch C: production gate

- Phase 5 validation
- Phase 6 full golden evaluation
- Phase 7 Google Sheets retest

## Non-goals

- model-controlled Google Sheets operations;
- dynamic column naming or mapping;
- automatic catalog creation from model output;
- silent aggregation of source item rows;
- broad SBIS changes before the photo pipeline passes its release gate.

## Definition of done

The work is complete when the five source photos are processed as four logical
documents, all golden metrics pass, every provider and model step is visible in
live logs, failed pipelines cannot write to Google Sheets, and a live test
places normalized rows under the unchanged `Накладная` headers.
