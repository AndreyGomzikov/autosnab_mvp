---
title: Workbook Export 3 Analysis
source: Копия АвтоСнаб Кафе Ромашка 3.xlsx
compiled_from: [src_d799d44293]
created: 2026-07-05
updated: 2026-07-05
tags: [analysis, workbook, google-sheets, validation]
status: current
---

# Workbook Export 3 Analysis

## Scope

This page analyzes the workbook `Копия АвтоСнаб Кафе Ромашка 3.xlsx`, which
contains the latest exported `Накладная` rows produced from scanned documents.

The goal is not to restate the analyst comments abstractly, but to tie each
remark to the actual row output now visible in the workbook.

## Confirmed document blocks

The sheet `Накладная` contains the expected row-2 machine contract and several
document blocks relevant to Lilia's feedback:

- row 3: `УПМК3003248`, `ТОРГ-12`, duplicate = `Да`
- row 8: `UPMK3003248`, `ТОРГ-12`, duplicate = `?`
- rows 11-17: receipt `0245`
- row 19: `1928`, `УПД`
- rows 21-27: receipt `ЧЕК 0245`
- rows 29-30: `УПМК3003248`, older block variant
- row 32: `УТ-35634`, `УПД`
- row 34+: second page/item continuation for `УПМК3003248`

This confirms the workbook is mixing multiple historical runs of similar
documents, not only one clean export per source file.

## Findings tied to Lilia's remarks

### 1. `Грузоотправитель` / `Получатель` are semantically wrong in supplier documents

Confirmed in multiple rows:

- row 3: both fields = `ЛИР ООО`
- row 8: both fields = `ЛИР ООО`
- row 19: both fields = `ООО "ЛИР"`
- row 32: both fields = `ООО "ЛИР", 236038 ...`

These values look like the buyer / own legal entity, not a true shipper and
consignee split from the source document. This matches Lilia's question about
why the waybill seems to be written to another company.

Implementation consequence:

- these columns are currently being filled by the wrong semantic source and
  cannot be derived by blind copying from recipient-side data.

### 2. `Основание` is still polluted by document-form text

Confirmed:

- row 19: `Универсальный передаточный документ No1928 от 23 июня 2026 г`
- row 32: `Универсальный передаточный документ, № УТ-35634 от 23 июня 2026 г`

Those values restate the document form + number instead of a contractual
basis. This directly confirms Lilia's remark that `Основание` is being filled
with data that belongs in `Форма документа`.

Counterexample:

- rows 3 / 8 / 29 correctly show `Основной договор`

So the bug is not universal; it depends on document/form parsing path.

### 3. The `Нет в справочнике` rule is not applied consistently

Confirmed mismatch:

- row 32 has `Товар найден в справочнике = Нет`, but `Корректировка = Другое`
- row 27 has `Товар найден в справочнике = Нет` and should therefore drive the
  explicit business rule `Корректировка = Нет в справочнике`

This matches Lilia's comment that unmatched products must set the correction
reason explicitly, rather than falling into a generic bucket.

### 4. Fallback `Наименование товара в УС` is still too noisy for package rows

Confirmed:

- row 17: source item `... ГАКЕТ-МАЙКА ВИКТОРИЯ 65*40СМ`
- row 17 fallback US name = `Пакет-майка Виктория`
- row 27 fallback US name = `Пакет-майка Виктория`

This matches the analyst comment that the catalog appears to contain a simpler
target item such as `Пакет пэт`, while the sheet shows a noisier fallback taken
from source text.

Implementation consequence:

- fallback name generation is still source-text-biased and not sufficiently
  normalized for operator review.

### 5. Receipt rows still miss `Цена в УС` and VAT columns

Confirmed for receipt blocks:

- rows 11-16 and 21-26: `Цена в УС` is empty
- same rows: `Ставка НДС` and `Сумма НДС` are empty

At the same time:

- `Кол-во в УС = 0.8`
- `Ед.изм. в УС = кг`
- `Цена за ед-цу = 72.9`

So the workbook proves that the conversion pipeline already writes accounting
quantity for receipt items, but still leaves accounting price and VAT fields
unfinished. This directly confirms Lilia's kefir/receipt remarks.

### 6. A conversion bug is visible on one `ТОРГ-12` block

Confirmed:

- row 8: `Кол-во в документе = 3.954`, but `Кол-во в УС = 15.634116`
- row 8: `Цена за ед-цу = 2250`, but `Цена в УС = 569.044006`

Compare with the same document in rows 3 and 29:

- `Кол-во в УС = 3.954`
- `Цена в УС = 2250`

This is hard evidence for Lilia's question about why `Кол-во в УС` became
multiple times larger even though the item name does not visibly specify such a
pack multiplier.

Implementation consequence:

- the same source document has been exported under two different conversion
  interpretations, which means the current coefficient logic is not stable.

### 7. The workbook contains duplicate document variants with inconsistent identity

Confirmed examples:

- row 3: document number `УПМК3003248`
- row 8: document number `UPMK3003248`
- row 29: document number `УПМК3003248`

and:

- row 11: receipt number `0245`
- row 21: receipt number `ЧЕК 0245`

This explains why duplicate markers oscillate between `Да`, `?`, and separate
blocks for what appears to be the same logical source document.

Implementation consequence:

- document identity normalization is still too weak before dedupe.

## Main conclusions

The workbook confirms that the analyst comments are grounded in current output,
not in hypothetical edge cases.

The most important proven defects are:

1. wrong semantic source for `Грузоотправитель` / `Получатель`
2. incorrect filling of `Основание` on the UPD path
3. inconsistent `Нет в справочнике` correction mapping
4. noisy fallback `Наименование товара в УС` for package rows
5. receipt-specific gaps: empty `Цена в УС`, empty VAT columns
6. unstable conversion logic across historical exports of the same document
7. weak document-number normalization before duplicate classification

## Priority for implementation

Based on this workbook, the next engineering pass should focus on:

1. semantic field mapping
2. deterministic unmatched-product correction reasons
3. receipt-specific accounting mapping
4. coefficient/price stability
5. document-identity normalization before dedupe

These issues are more urgent than separator-row cosmetics, because they affect
operator trust in the actual business values written into `Накладная`.
