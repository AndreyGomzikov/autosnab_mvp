---
title: Validation Table Behavior
source: inbox/Созвон с Лилией.md
source_hash: ffd34f080314822f
compiled_at: 2026-07-03T16:20:00+00:00
compiled_from: [src_ffd34f0803, src_137b730e1a, src_239bf1096e, src_4c95377e5e, src_121c42f2ea]
created: 2026-07-03
updated: 2026-07-04
tags: [table, validation, workflow, spreadsheet]
status: current
---

# Validation Table Behavior

## Role of the table

The working Google Sheet is an intermediate validation layer between the primary document and the accounting system.

The user does not upload directly into accounting. They first review the recognized document in the table, fix issues, and only then trigger loading.

The call with Lilia makes one more thing explicit: she demonstrated the intended operator workflow in the table itself, not just field definitions. For MVP purposes, the table behavior is as important as OCR quality.

## Header and script constraints

- the second row contains the effective column names used by the Apps Script and integration logic
- those names must stay stable
- warning/blocking around header renames is desirable because both script logic and code are name-bound
- the upper explanatory row must also be read as a business-annotation layer: it explains how a column is intended to be filled even when row 2 already defines the machine name
- row 2 remains the machine-bound header contract, but row 1 is not disposable noise; it is part of the offline interpretation contract for backend mapping

## Observed real sheet contract

The root workbook copy reviewed on 2026-07-04 makes the current working contract more concrete:

- the authoritative operator sheet is `Накладная`
- row 1 contains explanatory/business fill rules and must be considered during contract analysis
- row 2 is the machine-bound contract used for column lookup and Apps Script integration
- the actual business contract spans `A:AN` (`40` columns), from `Статус загрузки` to `Ссылка на исходный документ`
- the workbook also contains helper/automation columns in `AO:AU`; some of them mirror status/duplicate/correction logic and should be preserved rather than overwritten by backend payload shaping
- reference tabs already exist for suppliers, own-company/trade-point/warehouse mapping, products, and pack-size conversions: `Поставщики`, `Наша фирма`, `Товары`, `Справочник фасовок`

Important implementation consequence:

- the backend should write the document block against the row-2 contract in `A:AN`
- backend interpretation of how each target column should be filled must consider row 1 annotations together with row 2 machine names
- formulas, validations, and helper logic outside that contract should be treated as spreadsheet-owned behavior

The local `.xlsx` export also shows broken dropdown references (`#REF!`) for some list validations. That strongly suggests the live Google Sheet, not an exported `.xlsx`, must remain the final source of truth for named ranges and Apps Script behavior.

### Canonical workbook decision as of 2026-07-06

The local raw intake now contains the original workbook:

- `src_bd91ee3517` -> `../autosnab_mvp_raw/inbox/АвтоСнаб Кафе Ромашка  (ориг).xlsx`

This changes the evidence priority for future contract work:

- the original workbook should be treated as the canonical offline workbook source for the `Накладная` contract;
- the contract should be read from the combination of row 1 annotations, row 2 machine headers, reference tabs, and the immutable Apps Script workflow;
- workbook copies such as `Копия АвтоСнаб Кафе Ромашка 2.xlsx` and `Копия АвтоСнаб Кафе Ромашка 3.xlsx` remain useful for diagnosing historical bad exports, but they are not the contract authority;
- backend write rules should now be validated first against the original workbook structure, then against the live Apps Script behavior, and only after that against historical export copies.

Practical consequence:

- when two historical exports of the same invoice disagree, prefer the original workbook's row-2 headers, reference tabs, and document layout as the baseline, not the accidental shape of a past generated block.
- Apps Script is part of the table/workflow contract, not part of source-document parsing; OCR/MinerU/OpenAI still parse the invoice itself, while Apps Script constrains how parsed data must behave inside the sheet.

The extracted per-column mapping from the original workbook is now frozen in
`docs/wiki/original-workbook-contract.md`.

## Source document fields

The document flow is expected to populate, at minimum, fields like:

- document date
- document number
- supplier
- supplier INN
- recipient
- basis / contract
- line item name
- unit
- quantity
- price
- totals

Trade point and warehouse may sometimes come from the source document, but often need user selection from reference data.

## Status logic

The source call and workbook show document-level gating through statuses:

- normal recognized document -> user review -> ready for loading
- hard duplicate -> not ready
- possible duplicate -> requires manual check
- OCR/read failure -> not ready, stop further automation

The `Загрузка` / upload state should act at document level, not just line level.

The demonstrated operator logic was roughly:

- file is loaded from paper photo, scan, or EDO export
- document fields and rows are placed into the table
- the document receives an initial status
- the user checks recognition, duplicate markers, supplier mapping, and product mapping
- only documents that satisfy the status conditions are allowed into the test/accounting upload step

This means your MVP should optimize for controllable review states, not just for producing rows.

### Exact transition rules from the updated instruction

After initial load:

- clean recognition: `Статус загрузки = Проверить`, `Статус строки = Распознано`
- manual issue found: `Корректировка = Нет в справочнике / Исключение / Сопоставление / Другое`, `Статус загрузки = Требует проверки`, `Статус строки = Правка вручную`
- OCR failure: `Корректировка = Ошибка OCR`, `Статус загрузки = Не готово`, `Статус строки = Ошибка загрузки`
- confirmed duplicate on intake: `Дубль = Да`, `Статус загрузки = Не готово`, `Статус строки = Распознано`
- possible duplicate on intake: `Дубль = ?`, `Статус загрузки = Требует проверки`, `Статус строки = Распознано`

During operator correction:

- `Корректировка` is filled only on the specific item row with the problem
- after fixing data, the operator restores `Статус строки = Распознано` in the first row of the document block
- if duplicate ambiguity is resolved as false alarm, the operator clears `Дубль`, can restore `Статус загрузки = Проверить`, sets the upload checkbox, and runs `Проверить выбранные документы`

After document check:

- no remaining problems: `Статус загрузки = Загрузить`, `Статус строки = Готов к загрузке`, `Корректировка` is cleared
- still unresolved manual issues: `Статус загрузки = Требует проверки`, `Статус строки = Правка вручную`
- OCR error or confirmed duplicate still present: `Статус загрузки = Не готово`

After upload to the test sheet / accounting system:

- only documents with `Статус загрузки = Загрузить` and `Статус строки = Готов к загрузке` are eligible
- successful send sets `Статус загрузки = Загружено` and `Статус строки = Отправлено в УС`
- the upload checkbox is cleared automatically

After `Вернуть на проверку`:

- `Статус загрузки = Требует проверки`
- `Статус строки = Возврат на проверку`
- after edits, the operator restores `Статус строки = Распознано` and repeats the normal check/load cycle

## Duplicate handling

- exact duplicate should be blocked automatically
- possible duplicate can be marked with a question mark or equivalent review signal
- the user can clear the ambiguity and move the document back into the normal review path

Important nuance from the call: duplicate handling is based on document requisites, and the decision to load is made at document level. A duplicate document should not partially slip through because some individual rows look valid.

## Accounting-system export boundary

Only a subset of columns should go to the accounting system. The table itself contains more operator-facing and validation-oriented fields than the final accounting payload.

Important reference dimensions visible in the workbook:

- supplier directory
- own-company / trade-point / warehouse directory
- product directory
- pack-size / conversion rules

In practice, Lilia showed that the table is doing three jobs at once:

- capture recognized values from the source document
- let the user finish missing business mappings from references
- compute/export the final fields that should go further into the accounting path

So the table is not just a dump of OCR output. It is a transformation and approval surface.

## Special document forms

The call notes explicitly distinguish cases like cash receipts and purchase acts:

- supplier and supplier INN may stay empty there
- those forms follow a different accounting path
- they should not be forced through the same assumptions as standard UPD / TORG-12 supplier documents

This is a real MVP scope trap. If you hard-code one strict supplier-document contract, you will break on these forms early.

## Recheck / correction loop

The future `Вернуть на проверку` action is part of the intended workflow:

- a previously uploaded document may need correction
- the row/document returns to review
- user edits the data
- the document is checked and loaded again
- downstream accounting integration should treat this as a correction/rewrite, not just a blind duplicate insert

This means you need stable document identity and a way to tell "new upload" from "correction of an already loaded document."

## Packing and conversion logic

The workbook also makes one design point explicit: conversion factors should be computed by the program when pack size and unit are known, not entered manually by the user.

This affects:

- normalized quantity
- unit price in accounting units
- transparency of recalculation during review

The workbook also hints at an exception dictionary for piece-based products like eggs, lemons, limes, avocado, etc. So a naive generic converter will not be enough for all rows later.

The later source `Расчет коэфф.md` fixes the formula and implementation
boundary more precisely:

- coefficient means accounting units contained in one document unit;
- `quantity_us = quantity_document * coefficient`;
- `price_us = price_document / coefficient`;
- the converted quantity and price must preserve the source line amount;
- standard factors are computed from package value and units by backend code;
- piece-to-weight relations use an explicit product exception reference;
- ambiguous exception rows, such as two active avocado weights without a
  qualifier, must produce `Сопоставление` instead of an automatic choice.

The full conversion contract and unresolved business decisions are recorded in
`unit-conversion-rules.md`.

### Current deterministic implementation

OpenAI returns package and normalized-name candidates, but backend code
re-extracts codes/package values and recomputes the multiplier. The supported
base conversions are grams to kilograms, milliliters to liters, kilograms,
liters, and pieces; compound forms such as `0,5Л 12ШТ` are multiplied.

When the configured shared Google spreadsheet is available, backend reads:

- `Товары!A:D` by its fixed headers
- `Справочник фасовок!A:M` by its fixed headers and variants

An exact/unambiguous deterministic match fills the product name, accounting
unit, and accounting quantity. A missing product gets `Нет в справочнике`;
an ambiguous product, missing package, or incompatible unit gets
`Сопоставление` on that item row. The model never selects target columns.

The later workbook copy `Копия АвтоСнаб Кафе Ромашка  (2).xlsx` exposed an
operational nuance: older stored review payloads may lack `us_product_name`
and `product_found` even when the current deterministic matcher can now find
them. Because of that, building the `Накладная` sheet should include a backend
backfill step that re-merges parser item metadata and re-runs product/package
mapping before output when US fields are still empty.

The later screenshot `img.png` tightened that rule further: even when a row is
still unresolved against `Товары`, the `Наименование товара в УС` column should
show the deterministic normalized candidate name rather than staying blank.
This makes the operator workflow stable: `Товар найден в справочнике` can stay
`Нет` or `?`, but the normalized target name should still be visible.

The same screenshot also confirmed that `ИНН Поставщика` in the shared sheet
cannot rely only on previously stored header metadata, because older OCR/OpenAI
payloads may contain merged `ИНН/КПП` strings. Shared-sheet header build should
therefore normalize supplier INN again before rendering rows.

## Analyst feedback from 2026-07-05 screenshot

The later screenshot `img_2.png` refines the write contract further with
operator-visible acceptance criteria:

- `Грузоотправитель` and `Получатель` should not be blindly copied from the
  same counterparty fields if the source document is issued for another legal
  entity; mapping needs document-form-aware rules and explicit fallbacks.
- If a product is not found in `Товары`, `Причина корректировки` should be
  `Нет в справочнике` on that row. This is now a business-confirmed rule, not
  only an implementation guess.
- The fallback for `Наименование товара в УС` should prefer the closest
  normalized catalog-facing candidate and avoid noisy package strings such as
  `Пакет-майка Виктория` when the catalog item is simply `Пакет пэт`.
- The empty separator row between document blocks may stay, but its visual
  height should be reduced if the Google Sheets API/app-script path allows it
  without harming document separation.
- Quantity and price conversion must be explainable from package evidence. If
  the source row does not explicitly support the computed multiplier, the row
  should surface a review reason rather than silently inflating `Кол-во в УС`.
- `Цена в УС` must be filled whenever deterministic conversion succeeds; the
  kefir example confirms the current row-level conversion pipeline is still
  incomplete.
- Retail receipt documents need a separate extraction/mapping branch for VAT
  and possibly other fiscal fields; the screenshot confirms this cannot rely on
  the generic supplier-invoice assumptions.
- A row that visually satisfies the status prerequisites but still cannot pass
  `Проверить` indicates a mismatch between the backend write contract and the
  script's actual gating conditions. This needs explicit diagnosis against the
  live sheet behavior, not only backend reasoning.
- `Основание` should not be auto-filled with the document form name when the
  form already has its own dedicated column (`Форма документа`); those fields
  carry different semantics.
- `Госсистема` and `Дата приема` remain later-stage fields and should stay out
  of the initial intake payload unless a specific source adapter truly knows
  them.

## Practical MVP attention points

For the nearest MVP, the main things to watch are:

- build around document-level status transitions, not row-only transitions
- keep the target header contract stable and tied to the real sheet, not to ad hoc renamed columns
- separate three layers clearly: raw OCR fields, operator mapping fields, final export fields
- treat duplicate detection as a gate before upload
- support manual completion of trade point and warehouse from references
- allow OCR failure to still produce a reviewable document shell instead of hard-failing the flow
- do not assume every document has supplier and INN
- keep room for a future `Вернуть на проверку` / correction cycle
- avoid manual entry of conversion coefficients when they can be computed
- populate document-level statuses only in the first row of each inserted document block
- keep `Корректировка` row-specific instead of copying it across the whole block
- preserve the live sheet's formulas, validations, checkboxes, and helper columns rather than trying to recreate them in backend payloads

## Current shared-sheet insertion bug

The comparison between the original `АвтоСнаб Кафе Ромашка .xlsx` and the tested `Копия АвтоСнаб Кафе Ромашка .xlsx` shows:

- old document blocks are not being overwritten directly
- new document blocks are inserted at the top as intended
- one blank separator row is also inserted between blocks

However, the inserted rows are mapped against the wrong column contract.

### What is happening

The backend currently builds rows in the old `Накладные` register order, then inserts those values into the shared `Накладная` sheet without remapping to the real column order of that sheet.

### Visible symptom

Examples from the tested copy:

- upload timestamp lands under `Статус загрузки`
- internal document id lands under `Статус строки`
- document form lands under `Дубль`
- document date lands under `Форма документа`
- document number lands under `Загрузка`
- supplier-related values shift into the next business columns
- line item fields also shift left/right relative to the real `Накладная` sheet contract

### Practical conclusion

The current bug is not "prepend vs overwrite". The real bug is:

- the top-insert behavior works
- the row payload shape does not match the target sheet header order

So the next fix must be a dedicated mapper for the real `Накладная` sheet, not another change to row insertion mechanics.
