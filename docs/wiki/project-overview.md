---
title: autosnab_mvp Overview
source: inbox/Копия План АвтоСнаб .md
source_hash: aaf3469488312b75
compiled_at: 2026-07-03T14:39:13+00:00
compiled_from: [src_aaf3469488, src_ffd34f0803, src_137b730e1a]
created: 2026-07-02
updated: 2026-07-03
tags: [overview, mvp, product]
status: current
---

# autosnab_mvp Overview

`autosnab_mvp` is a document-processing and supplier-data platform for HoReCa operations.

## Current product center

The immediate delivery track is invoice / primary-document intake:

- user uploads a photo, scan, PDF, or later an EDO file
- the system extracts document fields and line items through a local extraction backend
- OCR remains the default backend, and MinerU is now available as the local high-structure backend for harder documents
- data lands in a validation table
- a user checks, fixes, and then sends the result into the accounting system

This is the active MVP path and the nearest business priority.

## Broader product direction

The larger system is not limited to invoice ingestion. It is intended to grow into a supplier-aware procurement platform:

- supplier price-list intake through a Telegram bot
- orchestration through `n8n`
- AI-assisted normalization of supplier items
- persistence in a DB as the canonical source
- Google Sheets as the early operator interface
- later web flows for suppliers, cafes, and purchasers

## Architectural direction

The repo should keep one shared document/data core and avoid source-specific forks:

- PDF/photo uploads are one source adapter
- SBIS EDO is the next source adapter
- MinerU is another extraction backend feeding the same document core
- supplier price ingestion is another adjacent intake flow
- business rules, normalization, status handling, and exports should stay centralized

## Near-term boundary

- This week: reliable document recognition and placement into the existing validation table
- Before SBIS: stabilize multi-page UX, parsing behavior, table/status logic, and the MinerU/OCR extraction boundary
- Next phase: SBIS EDO as a read-only adapter into the same document flow

## Longer-term roadmap signals

The new planning notes add two major future tracks:

- a managed supplier catalog with current prices and update history
- smarter product discovery, eventually including hybrid/vector and multimodal search after catalog normalization exists
