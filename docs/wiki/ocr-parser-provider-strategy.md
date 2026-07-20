---
title: OCR and Parser Provider Strategy
source: session
created: 2026-07-05
updated: 2026-07-05
tags: [architecture, ocr, llm, providers]
status: current
---

# OCR and Parser Provider Strategy

## Working idea

The next architecture step should keep OCR/layout extraction separate from the
final document parser.

Target shape:

`OCR / layout provider -> normalized evidence -> pluggable parser backend`

This is preferable to binding one OCR provider to one LLM provider or replacing
the whole current path with one monolithic vendor.

## Why this matters for autosnab_mvp

The current pipeline already proved that document quality problems and parsing
problems are not the same problem:

- bad photos, skew, broken runtime dependencies, and OCR transport failures
  damage evidence collection;
- item normalization, duplicate logic, and table field semantics are a separate
  downstream layer;
- one parser backend should be replaceable without rewriting Google Sheets
  mapping or deterministic validation.

So the stable boundary should be:

1. collect text/layout evidence from document pages;
2. normalize it into one internal evidence contract;
3. send that contract to one selected parser backend;
4. keep deterministic normalization and Google Sheets writing unchanged.

## Candidate providers to evaluate

### OCR / layout layer

- `Yandex Vision OCR`
  Strong candidate for image/PDF OCR, table-aware recognition, and async
  document processing.
- current providers remain valid fallback/reference sources:
  `Google OCR`, `MinerU`, and local image preparation.

### Parser layer

- `YandexGPT`
  Strong candidate because of async API shape and structured output for strict
  JSON-oriented document parsing.
- `GigaChat`
  Strong candidate as an alternative parser backend for Russian business
  documents, especially for A/B comparison on receipts and supplier invoices.
- current `OpenAI` path remains the active baseline until another provider is
  proven on the same golden set.

## Recommended evaluation strategy

Do not switch the whole product to a new vendor in one step.

Instead:

1. keep the internal normalized evidence contract;
2. add pluggable OCR providers behind one interface;
3. add pluggable parser backends behind one interface;
4. run the same golden set through:
   - current OpenAI path
   - Yandex Vision OCR + current parser
   - Yandex Vision OCR + YandexGPT
   - Yandex Vision OCR + GigaChat
5. compare:
   - field accuracy
   - table-row accuracy
   - receipt behavior
   - latency
   - operational reliability
   - cost

## Practical conclusion

The idea to use OCR first and LLM second is sound.

The most promising vendor exploration shape is:

- `Yandex Vision OCR` as a likely primary OCR/evidence provider;
- `YandexGPT` as a likely async structured parser candidate;
- `GigaChat` as an alternative parser backend for Russian document quality
  comparison.

For the repository architecture this should mean provider abstraction, not a
hard-coded vendor replacement.
