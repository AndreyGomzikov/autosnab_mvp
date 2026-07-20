---
title: Unit Conversion Rules
source: raw
compiled_from: [src_3b4148378c]
created: 2026-07-04
updated: 2026-07-04
tags: [invoices, normalization, units, conversion, pricing]
status: current
---

# Unit Conversion Rules

## Business requirement

The user must not enter the conversion coefficient manually when package size
and units are known. Backend code must calculate the coefficient, accounting
quantity, and accounting unit price before rows reach the quality-control
sheet.

The model may return package and conversion candidates, but they are evidence
only. The deterministic conversion engine is authoritative.

## Coefficient definition

Use one unambiguous definition throughout the system:

```text
k = quantity in the accounting unit contained in one document unit
```

Examples:

| Document package | Accounting unit | k |
| --- | --- | ---: |
| 250 g | kg | 0.25 |
| 980 ml | l | 0.98 |
| 5 l | l | 5 |
| 0.25 kg | kg | 0.25 |

For a document row:

```text
quantity_us = quantity_document * k
price_us = price_document / k
```

The price formula is valid only when `price_document` is the price for one
`document_unit` and `k > 0`.

The invariant is:

```text
quantity_document * price_document
≈ quantity_us * price_us
```

The comparison must use the same VAT basis as the source price and configured
rounding tolerance.

## Deterministic conversion order

1. Normalize document and accounting units.
2. If both units are the same and no package conversion is needed, use `k=1`.
3. Calculate standard physical conversions from package data:
   - g to kg: `value / 1000`;
   - kg to kg: `value`;
   - ml to l: `value / 1000`;
   - l to l: `value`;
   - pieces to pieces: `value`;
   - compound packages: multiply package size by contained unit count.
4. Use an exact active product exception only when standard conversion cannot
   describe the relation, for example `piece -> kg`.
5. If inputs conflict or no unique conversion exists, do not guess. Leave
   converted numeric values empty and add row correction `Сопоставление`.

## Exception reference

The source document proposes average weight per piece, for example:

| Product | kg per piece |
| --- | ---: |
| Яйцо С0 | 0.065 |
| Яйцо С1 | 0.055 |
| Яйцо С2 | 0.045 |
| Лимон | 0.120 |
| Лайм | 0.080 |
| Авокадо | 0.350 or 0.400 |

This must be a deterministic reference, not prompt knowledge. A safe exception
record needs at least:

- stable ID;
- normalized product or product ID;
- document unit;
- accounting unit;
- factor value;
- optional package/variant/supplier qualifier;
- active flag;
- effective dates;
- source/comment.

Two active avocado values without a distinguishing qualifier are ambiguous.
The backend must require manual matching instead of choosing one.

## Existing package reference

`Справочник фасовок` should identify package aliases and conversion inputs.
Its existing `Коэффициент пересчета` can be retained for compatibility, but the
backend should recompute the factor from package value and units whenever
possible.

If the stored and computed factors differ:

- use neither value silently;
- add a review flag;
- expose both values in debug trace;
- require correction of the reference data.

## Result contract

Each normalized item should carry:

- `conversion_factor`;
- `conversion_method`: `identity`, `standard`, `compound_package`,
  `product_exception`, or `unresolved`;
- `conversion_source_id`, when a reference row was used;
- `document_unit`;
- `accounting_unit`;
- `quantity_document`;
- `quantity_us`;
- `price_document`;
- `price_us`;
- `conversion_review_reason`;
- calculation inputs and unrounded values in debug metadata.

Use `Decimal` internally. Round only at the explicit output boundary according
to the accounting system's quantity and price precision.

## Current implementation gap

Current backend behavior is partial:

- standard package conversion and `quantity_us` already exist;
- package matching can read a stored coefficient from
  `Справочник фасовок`;
- `Цена в УС` is currently always written as an empty value;
- there is no dedicated product exception reference;
- there is no amount-preservation check for converted price and quantity.

## Required tests

- `250 g -> 0.25 kg`;
- `980 ml -> 0.98 l`;
- `5 l -> 5 l`;
- `0.25 kg -> 0.25 kg`;
- identity conversion;
- compound package such as `0.5 l x 12 -> 6 l`;
- egg `С1`, document quantity in pieces, accounting quantity in kg;
- ambiguous avocado exception;
- stored package coefficient conflicting with computed coefficient;
- zero/negative factor rejection;
- quantity/price amount invariant;
- correct `Кол-во в УС` and `Цена в УС` sheet mapping;
- unresolved conversion produces `Сопоставление` only on the affected row.

## Open questions before production rollout

- Required quantity and price precision in the target accounting system.
- Whether `Цена в УС` must be calculated from document unit price or from the
  line amount divided by `quantity_us` when source rounding differs.
- Where the product exception reference will live: a new Google Sheet tab or a
  versioned backend table.
- Which qualifiers distinguish multiple valid weights for one product, such as
  avocado size, variety, supplier, or package.
