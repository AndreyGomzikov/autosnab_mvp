---
title: Backend vs Original Workbook Gap Analysis
source: session
compiled_from: [src_bd91ee3517]
created: 2026-07-06
updated: 2026-07-06
tags: [backend, spreadsheet, gap-analysis, google-sheets]
status: current
---

# Backend vs Original Workbook Gap Analysis

## Goal

Compare the current backend write path against the canonical workbook contract
frozen in `original-workbook-contract.md`.

Scope of comparison:

- original workbook `Накладная` contract (`row 1 + row 2`)
- immutable Apps Script workflow assumptions
- current backend write path in:
  - `backend/app/services/invoice_review_service.py`
  - `backend/app/services/google_sheets_service.py`
  - `backend/app/services/item_normalization_service.py`
  - `backend/app/services/invoice_normalization_service.py`

## Main architectural finding

The shared-sheet write path now builds `Накладная` rows natively.

Current shared-sheet flow is:

```text
normalized/header+item metadata
-> build_shared_invoice_rows(...)
-> shared Google Sheet "Накладная"
```

The old register path still exists, but it is now a compatibility layer for
legacy review output rather than the active shared-sheet write path.

Implementation consequence:

- the dominant translation drift between identical invoices and the final
  shared sheet is reduced;
- the remaining gaps are now mostly field-ownership and contract-centralization
  issues, not the existence of an obligatory legacy remap stage.

## What already matches the canonical workbook

### 1. Row-2 target headers are explicitly enforced

`google_sheets_service.py` verifies row-2 headers via `_read_target_headers(...)`
and expects the shared-sheet contract across `A:AN`.

This is aligned with the original workbook decision that row 2 is the
machine-binding layer.

### 2. First-row-only behavior is already encoded

The direct shared builder emits first-row-only fields only on the first item
row of a document block, including:

- `Статус загрузки`
- `Статус строки`
- `Дубль`
- `Форма документа`
- `Дата документа`
- `№ Документа`
- `Поставщик`
- `ИНН Поставщика`
- `Грузоотправитель`
- `Получатель`
- `Торговая точка`
- `Склад`
- `Основание`
- `Сумма накладной`
- `Время загрузки документа`
- `ID документа`
- `Ссылка на исходный документ`

This is compatible with the Apps Script boundary detection model.

### 3. `Основание` cleanup exists upstream

`invoice_normalization_service.py` already clears `basis` when it merely echoes
document-form text or document number/date.

This is aligned with the original workbook rule that `Основание` is a true
business basis field, not a duplicate of `Форма документа`.

### 4. Deterministic correction normalization exists

`item_normalization_service.py` already upgrades:

- product miss -> `Нет в справочнике`
- ambiguous match -> `Сопоставление`

and `google_sheets_service.py` reinforces:

- `product_found == Нет` -> `Корректировка = Нет в справочнике`
- `product_found == ?` and blank/generic correction -> `Сопоставление`

This matches the analyst-confirmed correction semantics.

## Confirmed gaps against the canonical workbook

### Gap 1. Row-1 business annotations are still not represented as a formal field-spec module

Severity: high

The shared-sheet writer now writes `Накладная` directly, but row-1 semantics
still live implicitly across several services instead of one contract module.

Why this matters:

- row-1 is now part of the canonical contract;
- future fixes can still satisfy row-2 machine names while drifting from row-1
  business meaning.

Recommended change:

- create an explicit `Накладная` field-spec module:
  - machine header
  - row-1 meaning
  - source kind: parser / reference / formula / workflow / service
  - first-row-only flag

### Gap 2. The legacy register builder still exists in parallel

Severity: medium

`build_review_sheet(...)` still builds the old `INVOICE_REGISTER_HEADERS`
register in parallel with the new direct shared rows.

Why this matters:

- there are still two output vocabularies in the codebase;
- future fixes can accidentally patch only one of them.

Recommended change:

- keep the legacy register only where truly required;
- centralize shared field derivation so both outputs use the same contract data.

### Gap 3. `Загрузка` is treated as a carried field instead of a user-owned workflow field

Severity: medium

In the canonical workbook, row 1 for column `F` says the user selects it
manually during upload to accounting.

Current direct shared builder leaves `Загрузка` blank by default, which is
safer than the old remap path, but the ownership rule is still only implicit.

This means backend still propagates a legacy boolean/value into a field that
the original workbook frames as operator-controlled workflow state.

Why this matters:

- if backend writes this field inconsistently across runs, identical invoices
  can enter the Apps Script workflow differently;
- this field should likely be blank or normalized unless the workflow
  explicitly requires a preset value.

Recommended change:

- define a stricter ownership rule for `Загрузка`:
  - user-owned by default;
  - backend may only preset it deliberately and consistently.

### Gap 4. `Грузоотправитель` and `Получатель` still depend on weak upstream availability

Severity: medium

The direct shared builder no longer copies unrelated recipient-side values, but
these fields still depend on:

- `document_meta.get("shipper")`
- `document_meta.get("recipient")`
- or old row-map values

The canonical workbook says both are invoice-imported fields, not guessed
fallbacks.

Why this matters:

- inconsistent OCR/OpenAI extraction or stale review payloads can still produce
  different serialized rows for the same invoice.

Recommended change:

- treat missing shipper/recipient as empty plus explicit review signal;
- do not allow silent fallback from unrelated business fields.

### Gap 5. `Торговая точка` and `Склад` are workbook-level reference fields, but backend mapping is still too flat

Severity: medium

The original workbook says:

- `Торговая точка` and `Склад`
  are manual or intake-derived fields with `Наша фирма` matching

Current direct builder still serializes them as plain resolved values without a
first-class contract object that distinguishes parser guess, matched catalog
value, and operator override.

No explicit contract layer currently distinguishes:

- parser candidate;
- operator-selected value;
- matched canonical value from `Наша фирма`.

Recommended change:

- model these fields explicitly as reference-backed contract fields, not plain
  passthrough strings.

### Gap 6. Analytics columns `AG:AJ` are still unresolved operationally

Severity: medium

The direct shared builder now uses canonical names, but the underlying business
population for:

- `Кол-во в заявке`
- `Цена по прайсу`
- `Предыдущая дата поставки`
- `Предыдущая цена`
- `Отклонение от цены прайса`

is still mostly blank/pass-through in the current backend path.

Recommended change:

- implement these fields as explicit reference/analytics providers rather than
  leaving them as empty placeholders.

### Gap 7. Helper columns `AO:AU` are not represented as a protected boundary in code

Severity: low

The original workbook copy has empty headers in `AO:AU`, while the live Apps
Script may still use helper columns there.

Current writer limits direct value writes to `A:AN`, which is good. But the
contract is not yet formalized in code as:

- writable business range: `A:AN`
- spreadsheet-owned helper range: `AO:AU`

Recommended change:

- encode this boundary explicitly in shared-sheet contract constants and tests.

## Contract ownership by field group

The canonical workbook implies this ownership split:

- parser-owned input candidates:
  - `E`, `G:O`, `Q`, `S`, `U`, `W`, `Y:AB`
- deterministic/reference-backed backend fields:
  - `P`, `R`, `T`, `V`, `X`, `AF:AJ`, `AK`
- operator/workflow/service fields:
  - `A:F`, `AD:AN`

The current code approximates this split, but does not encode it as one
centralized contract. That is the main reason behavior can still drift.

## Recommended execution order

1. Add a native `Накладная` contract module from `original-workbook-contract.md`
2. Build `Накладная` rows directly, without going through `INVOICE_REGISTER_HEADERS`
3. Preserve the existing remap path only as a compatibility adapter
4. Explicitly classify each field as parser/reference/operator/service owned
5. Add tests that compare generated `Накладная` rows directly against the
   original workbook contract, not against the legacy register shape first

## Immediate coding target

If only one thing is changed next, it should be this:

- extract one explicit field-spec/ownership layer for `Накладная`

The direct shared builder already removed the largest translation drift. The
next gain now comes from centralizing contract semantics rather than from
adding yet another mapper rewrite.
