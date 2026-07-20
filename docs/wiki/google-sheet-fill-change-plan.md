---
title: Google Sheet Fill Change Plan
source: img_2.png
compiled_from: [ba-feedback-screenshot-2026-07-05]
created: 2026-07-05
updated: 2026-07-05
tags: [google-sheets, plan, validation, mapping]
status: current
---

# Google Sheet Fill Change Plan

## Why this plan exists

The business-analyst screenshot `img_2.png` confirms that the current shared
`Накладная` writer is structurally close, but several fields are still filled
with the wrong semantics or incomplete deterministic logic.

This is not a prepend/insertion problem anymore. The next pass should target
field meaning, conversion transparency, and compatibility with the live
`Проверить` workflow.

## Confirmed issues from the screenshot

1. `Грузоотправитель` / `Получатель` may currently reflect the wrong company.
2. Unmatched products must explicitly set `Причина корректировки = Нет в справочнике`.
3. Fallback `Наименование товара в УС` can still pick a noisy candidate instead
   of the intended normalized product name.
4. Separator rows are acceptable, but their visual height should be smaller if
   technically possible.
5. Some rows show unjustified growth in `Кол-во в УС`; conversion must be
   explainable from source package evidence.
6. `Цена в УС` is still missing for at least part of the receipt/kefir path.
7. Receipt rows do not yet provide stable VAT extraction/fill behavior.
8. A document row can visually look ready but still fail `Проверить`; the live
   gating logic is not fully mirrored by backend assumptions.
9. `Основание` is incorrectly populated by document-form text in some rows.
10. `Госсистема` and `Дата приема` should remain out of the intake writer for now.

## Target logic changes

### 1. Split field ownership by semantics

Adjust the mapper so these fields are treated separately:

- `Форма документа`: document-form classifier only.
- `Основание`: contract/basis text from the source document only.
- `Грузоотправитель`: shipper from the source document when present.
- `Получатель`: recipient from the source document when present.

Rules:

- never copy `Форма документа` into `Основание`;
- never blindly mirror supplier/recipient fields into shipper/consignee slots;
- when a field is unknown, keep it empty and attach a review warning instead of
  fabricating a value.

### 2. Tighten row-level correction reasons

Make row post-processing authoritative for unresolved catalog mapping:

- product not found -> `Причина корректировки = Нет в справочнике`;
- ambiguous match / package ambiguity / unit ambiguity -> `Сопоставление`;
- evidence too weak for reliable conversion -> keep row reviewable and surface a
  deterministic warning.

This must be row-specific and should not silently depend on stale stored review
payloads.

### 3. Improve fallback `Наименование товара в УС`

Refine fallback-name selection order:

1. exact deterministic catalog match
2. normalized name candidate without packaging noise
3. cleaned source item name
4. raw source item name

Additional rule:

- packaging tokens such as `майка`, `фас`, `упак`, sizes, and promo tails
  should not dominate the fallback name when the core product noun is known.

### 4. Make conversion output auditable

For each row, compute and persist:

- source unit
- package evidence
- coefficient source
- `Кол-во в УС`
- `Цена в УС`
- row-level conversion warning, when needed

Rules:

- if coefficient is deterministic, fill both `Кол-во в УС` and `Цена в УС`;
- preserve line amount within rounding tolerance;
- if the multiplier cannot be justified from the source row/package/reference,
  do not silently inflate `Кол-во в УС`; mark the row for review instead.

### 5. Add a receipt-specific branch

Receipt documents need their own normalization layer:

- supplier fields may be partial or absent;
- VAT may need to be extracted from fiscal sections or item-level patterns;
- product naming may differ from supplier invoices;
- accounting mapping still writes into the same `Накладная` contract, but the
  source extraction logic must not assume UPD/TORG-12 structure.

### 6. Align backend with the real `Проверить` gate

Run a targeted diagnosis of why a row with visible upload checkbox still cannot
pass `Проверить`.

Check at minimum:

- exact status values in first row of the block;
- whether the script expects document-level fields only in row 1;
- whether `Дубль`, `Корректировка`, or hidden helper columns still contain
  blocking values;
- whether checkbox/state updates are written back into the same cells the script
  reads.

Outcome required:

- one explicit backend-side readiness predicate that matches the live script's
  gating conditions.

### 7. Keep late-stage fields out of intake

Do not fill `Госсистема` and `Дата приема` during initial document write unless
their source adapter explicitly provides them.

These belong to a later operational stage and should not be guessed.

## Recommended implementation order

1. Fix semantic mapping for `Основание`, `Форма документа`,
   `Грузоотправитель`, and `Получатель`.
2. Tighten row-level correction reason assignment and fallback US-name cleanup.
3. Complete deterministic conversion so `Цена в УС` is filled together with
   `Кол-во в УС` when the coefficient is known.
4. Add receipt-specific extraction/normalization rules for VAT and weak supplier
   fields.
5. Diagnose the live `Проверить` gate against the real sheet/App Script.
6. Optionally reduce separator-row height if the Google Sheets integration can
   do it safely without affecting formulas or operator readability.

## Verification checklist

- A wrong-entity shipper/recipient case stays empty or correct, but never
  copies the wrong company automatically.
- A missing catalog match always yields `Нет в справочнике`.
- `Наименование товара в УС` shows a clean normalized fallback name, not a
  noisy package string.
- `Кол-во в УС` and `Цена в УС` are either both deterministically justified or
  the row is explicitly flagged for review.
- Receipt examples populate VAT/document fields according to their own branch
  without breaking normal invoice rows.
- `Основание` no longer duplicates `Форма документа`.
- A document that visually satisfies the ready state also passes the actual
  `Проверить` gate in the live sheet.

## Priority

This plan sits inside the current top-priority MVP track:

`document recognition -> deterministic normalization -> correct shared-sheet write -> operator review -> gated upload`

It should be executed before SBIS-specific work and before broader catalog
expansion, because the business issue is now correctness of the operator-facing
table contract.
