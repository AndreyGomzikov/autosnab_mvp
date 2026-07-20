---
title: Original Workbook Contract
source: inbox/АвтоСнаб Кафе Ромашка  (ориг).xlsx
compiled_from: [src_bd91ee3517]
created: 2026-07-06
updated: 2026-07-06
tags: [spreadsheet, contract, google-sheets, validation]
status: current
---

# Original Workbook Contract

## Scope

This page freezes the offline contract extracted from the original workbook
`АвтоСнаб Кафе Ромашка  (ориг).xlsx`.

The target sheet is `Накладная`.

Contract priority for backend work:

1. row 1 business annotations
2. row 2 machine headers
3. existing Apps Script workflow/gating behavior
4. historical exported copies only as regression evidence

Apps Script is part of the sheet workflow contract, not the invoice-parsing
layer. OCR, MinerU, and OpenAI still parse the source document itself.

## Structural findings

- workbook sheet order:
  - `Загрузка тест`
  - `Накладная`
  - `Поставщики`
  - `Наша фирма`
  - `Товары`
  - `Справочник фасовок`
  - `Лист2`
- the `Накладная` sheet has no merged cells in the header area
- row 1 and row 2 are both populated across `A:AN`
- helper columns `AO:AU` exist physically, but their row-1 and row-2 headers
  are empty in the offline workbook copy

## Layer meaning

### Row 1

Row 1 is the business-annotation layer.

It answers questions such as:

- is the field imported from the source document?
- is the field chosen manually by the user?
- is the field matched against a reference sheet?
- is the field calculated?
- is the field service-only?

### Row 2

Row 2 is the machine-binding layer.

It provides the actual header names that backend code and Apps Script should
use for column lookup.

## Column contract for `Накладная`

### Service and workflow columns

| Col | Row 2 header | Row 1 rule |
| --- | --- | --- |
| A | `Статус загрузки` | service-only, determined at load time |
| B | `Статус строки` | service-only, determined at load time |
| C | `Корректировка` | service-only, determined at load time |
| D | `Дубль` | status/script-formatting field, set at load time |
| E | `Форма документа` | determined during recognition; receipts/purchase acts may not yield supplier fields |
| F | `Загрузка` | selected manually by the user during upload to accounting |

### Document header fields

| Col | Row 2 header | Row 1 rule |
| --- | --- | --- |
| G | `Дата документа` | load from invoice; import to accounting |
| H | `№ Документа` | load from invoice; import to accounting |
| I | `Поставщик` | load from invoice; match against `Поставщики`; import to accounting |
| J | `ИНН Поставщика` | load from invoice; match against `Поставщики`; import to accounting |
| K | `Грузоотправитель` | load from invoice; EGAIS/alcohol declaration note present |
| L | `Получатель` | load from invoice; import to accounting |
| M | `Торговая точка` | manual or loaded from invoice depending on intake data; match against `Наша фирма`; import to accounting |
| N | `Склад` | manual or loaded from invoice depending on intake data; match against `Наша фирма`; import to accounting |
| O | `Основание` | load from invoice; import to accounting |
| AC | `Сумма накладной` | load from invoice; import to accounting |
| AD | `Дата приема` | determined at load time; user may correct manually |
| AE | `Принял, Ф.И.О.` | manual user field |
| AL | `Время загрузки документа` | service-only, determined at load time |
| AM | `ID документа` | service-only, determined at load time; sequential or random |
| AN | `Ссылка на исходный документ` | service-only, determined at load time |

### Item matching and quantity/price fields

| Col | Row 2 header | Row 1 rule |
| --- | --- | --- |
| P | `Товар найден в справочнике` | determined during invoice loading |
| Q | `Наименование товара из документа` | load from invoice |
| R | `Наименование товара в УС` | match against `Товары`; import to accounting |
| S | `Ед.изм. в документе` | load from invoice; import to accounting |
| T | `Ед.изм. в УС` | match against `Товары`; import to accounting |
| U | `Кол-во в документе` | load from invoice |
| V | `Кол-во в УС` | calculated from package/volume plus `Q/S/U`; import to accounting |
| W | `Цена за ед-цу` | load from invoice |
| X | `Цена в УС` | calculated from `AB / V` without VAT or `Y / V` with VAT; import to accounting |
| Y | `Стоимость без НДС` | load from invoice; import to accounting |
| Z | `Ставка НДС` | load from invoice; import to accounting |
| AA | `Сумма НДС` | load from invoice; import to accounting |
| AB | `Общая стоимость` | load from invoice; import to accounting |

### Reference and analytics columns

| Col | Row 2 header | Row 1 rule |
| --- | --- | --- |
| AF | `Госсистемы` | matched against `Товары` during invoice loading |
| AG | `Кол-во в заявке` | matched against `Заявка` by product, then supplier, then latest date |
| AH | `Цена по прайсу` | matched against `Прайс` by product, then supplier, then latest date |
| AI | `Предыдущая дата поставки` | determined at load time from prior deliveries by product and supplier |
| AJ | `Предыдущая цена` | matched against `Заявка` using `R` (`Наименование товара в УС`) |
| AK | `Отклонение от цены прайса` | formula: `(W - AH) / AH * 100%` |

## Data-validation dropdowns (ground truth for exact enum values)

Extracted from `ws.data_validations` in the original workbook (not just row-1/2
text). These are the exact strings and casing Google Sheets enforces — the
backend must produce values matching these exactly, not paraphrase them.

| Col | Field | Allowed values (exact) |
| --- | --- | --- |
| A | `Статус загрузки` | `Проверить, Загрузить, Загружено, Не готово, Требует проверки` |
| B | `Статус строки` | `Распознано, Правка вручную, Отправлено в УС, Ошибка загрузки, Возврат на проверку, Готов к загрузке` |
| C | `Корректировка` | `Сопоставление, Исключение, Нет в справочнике, Другое, Ошибка OCR` |
| D | `Дубль` | `Да, ?` |
| E | `Форма документа` | `Торг-12, УПД, Кассовый чек, Акт закупа, Акт приема-передачи, Транспортная накладная, Расходно-приходная накладная, Накладная` |
| P | `Товар найден в справочнике` | `Да, Нет` |

`Форма документа` is mixed-case `"Торг-12"`, not `"ТОРГ-12"`, and has no
`"Счет-фактура"`/`"Чек"` option — a 2026-07-09 backend fix (`ТОРГ-12`,
invented `Чек`/`Счет-фактура`) got this wrong on the first pass; corrected in
`invoice_normalization_service.py`'s `_normalize_document_form(...)`,
`ocr_service.py`'s `_extract_document_form(...)`, and
`invoice_review_service.py`'s `_detect_document_form_from_text(...)`. See
`docs/wiki/log.md` for the full trace.

The other five checked dropdowns already matched the backend's constants and
the Apps Script's `LOAD_STATUS`/`ROW_STATUS` exactly.

Helper columns AP:AS (41-44) carry a duplicate legend of these same status/
correction values for human documentation purposes — confirmed still not
part of the write contract, consistent with the AO:AU note above.

## Manually-filled example rows (ground truth for row shape)

Rows 3-16 contain five hand-filled example documents (mix of `Торг-12` and
`УПД`, various `Корректировка` states, several unit conversions: `бан→кг`,
`пак→л`, `уп→кг`, `кега→л`, `бут→л`/`бут`). Confirmed useful as a reference
for expected row shape:

- `Грузоотправитель` and `Получатель` are genuinely different companies in
  row 3 (`ООО "Балтика"` vs `ООО "Восток"`) — never the same value. Rows 7
  and 10 legitimately leave `Грузоотправитель` blank when the source
  document has no separate such line, rather than defaulting it to anything.
- `Получатель` is consistently `ООО "Восток"` (our own receiving entity)
  across all five examples, matching the interpretation that this column is
  the печатный `Грузополучатель`, not a copy of `Поставщик`/`Продавец`.

## Backend consequences

- row 2 remains the only stable binding for column lookup
- row 1 must constrain how backend fills row-2 fields; it is not optional text
- columns `A:F`, `AD:AN` are not raw parser output fields; they belong to
  workflow, operator action, or service metadata
- columns `R`, `T`, `V`, `X`, `AF:AJ`, `AK` are downstream mapping/calculation
  fields, not direct OCR/OpenAI extraction targets
- helper columns `AO:AU` should not be treated as part of the write contract
  until the live sheet/App Script proves otherwise

## First-row-only implications

The original workbook plus existing Apps Script behavior support this
interpretation:

- document-level fields belong in the first row of a document block
- item-level fields belong on every item row
- continuation rows should not repeat document-start fields such as
  `Дата документа`, `№ Документа`, and `Поставщик`, because the Apps Script
  uses those fields to detect document boundaries

## Immediate implementation use

This page should now drive:

- backend `Накладная` row builder validation
- first-row-only vs row-level field separation
- deterministic calculation boundaries for `V` and `X`
- reference-sheet dependencies for `R`, `T`, `AF`, `AG`, `AH`, `AJ`
- regression checks against historical bad exports such as
  `Копия АвтоСнаб Кафе Ромашка 3.xlsx`
