---
title: Supplier Catalog Roadmap
source: inbox/Копия План АвтоСнаб .md
source_hash: aaf3469488312b75
compiled_at: 2026-07-03T14:39:13+00:00
compiled_from: [src_aaf3469488]
created: 2026-07-03
updated: 2026-07-03
tags: [roadmap, suppliers, catalog, telegram, n8n]
status: current
---

# Supplier Catalog Roadmap

## Main idea

AutoSnab is expected to evolve beyond document intake into a supplier catalog and procurement platform.

The supplier-side MVP is: suppliers send or update price lists through a Telegram bot, the system normalizes the data, stores it in a DB, and exposes current prices to cafes and purchasers.

## Supplier price-list MVP

Target flow:

```text
Supplier
-> Telegram bot
-> n8n scenario
-> AI price-processing agent
-> database
-> Google Sheet "Товары от поставщиков"
-> cafe / purchaser sees current prices
```

### Supplier actions expected in MVP

- upload a price list as Excel / CSV / sheet
- send item data as text when no file is available
- update a previously uploaded price list
- add, change, archive, or remove items
- inspect current items

### Data expected from processing

- supplier item name
- supplier identity
- price
- unit
- pack size / volume / weight
- order multiple
- minimum order
- comments and supply conditions
- original raw data, normalized data, upload time, update time, source

### Storage direction

The DB should be canonical. Google Sheets is only the MVP operating surface.

Minimum entities mentioned in the source:

- supplier
- uploaded price list
- supplier item
- price
- unit
- item status
- upload/update timestamps
- source

## Why this matters for the current repo

This roadmap strengthens the case for a shared normalization core:

- incoming invoices and incoming supplier price lists are different sources
- both need raw preservation, normalization, traceability, and operator review
- the project should not hard-code everything around one invoice-only path

## Search roadmap

The same source places multimodal search after catalog normalization, not before.

Recommended sequence from the note:

1. build the supplier catalog
2. normalize items
3. match equivalent or near-equivalent products
4. add smart text search
5. add vector / hybrid search
6. add multimodal search: text + voice + photo
7. add analog / substitute / best-offer selection

## Practical implication

Current invoice/validation work is still the short-term priority, but the data model should avoid blocking the later supplier-catalog path.
