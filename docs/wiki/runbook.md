---
title: Runbook
source: session
created: 2026-07-03
updated: 2026-07-10
tags: [runbook, dev, qa, operations]
status: current
---

# Runbook

## Goal

This runbook covers the shortest practical path to:

- start the backend locally
- authorize Google OAuth
- upload a test invoice
- verify Google Sheets writing
- run local checks/tests

## Prerequisites

- Python `3.12` recommended
- access to the repo root
- Google Cloud OAuth client for Drive + Sheets
- edit access to the target Google Spreadsheet if shared-sheet mode is used

## Required secrets

```text
GOOGLE_OAUTH_CLIENT_ID
GOOGLE_OAUTH_CLIENT_SECRET
GOOGLE_OAUTH_ACCESS_TOKEN
GOOGLE_OAUTH_REFRESH_TOKEN
GOOGLE_OAUTH_TOKEN_EXPIRY
```

All OAuth secrets live in `.env`. Access token, refresh token, and expiry are
written there after the OAuth callback; the token fields may be empty before
the first authorization.

## Minimal `.env`

For local SQLite + OCR + Google Sheets:

```env
DATABASE_URL=sqlite:///./autosnab_mvp.db

GOOGLE_AUTH_MODE=oauth
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=
GOOGLE_OAUTH_ACCESS_TOKEN=
GOOGLE_OAUTH_REFRESH_TOKEN=
GOOGLE_OAUTH_TOKEN_EXPIRY=
GOOGLE_OAUTH_AUTH_URI=https://accounts.google.com/o/oauth2/auth
GOOGLE_OAUTH_TOKEN_URI=https://oauth2.googleapis.com/token
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/api/v1/google-oauth/callback
SECRETS_ENV_FILE=.env

GOOGLE_DRIVE_OCR_ENABLED=true
GOOGLE_DRIVE_OCR_LANGUAGE=ru
GOOGLE_DRIVE_OCR_DELETE_TEMP_FILES=true
GOOGLE_DRIVE_OCR_FOLDER_ID=

GOOGLE_SHEETS_ENABLED=true
GOOGLE_APPS_SCRIPT_ENABLED=false
GOOGLE_DRIVE_FOLDER_ID=
PUBLIC_API_BASE_URL=http://localhost:8000
UPLOADED_INVOICES_DIR=uploads/invoices

IIKO_INTEGRATION_ENABLED=false
IIKO_BASE_URL=
IIKO_LOGIN=
IIKO_PASSWORD_SHA1=
IIKO_TOKEN=
IIKO_TIMEOUT_SECONDS=30
IIKO_AUTO_MAPPING_ENABLED=true
IIKO_MAPPING_MIN_CONFIDENCE=0.72

# OpenAI parser over PDF/MinerU/OCR evidence
DOCUMENT_EXTRACTION_BACKEND=openai
DOCUMENT_EXTRACTION_FALLBACK_TO_OCR=true
MINERU_COMMAND={python_executable} -m mineru.cli.client -p {file_path} -o {output_dir} -b pipeline -l cyrillic
MINERU_TIMEOUT_SECONDS=900
OPENAI_API_KEY=<secret>
OPENAI_INVOICE_MODEL=gpt-5-mini
OPENAI_DEBUG_LOG_ENABLED=true
OPENAI_DEBUG_LOG_DIR=exports/openai_debug
```

For cloud `n8n`, the backend needs one public HTTPS base URL, for example
`https://example.ngrok-free.app`. In that setup set both:

```env
PUBLIC_API_BASE_URL=https://example.ngrok-free.app
GOOGLE_OAUTH_REDIRECT_URI=https://example.ngrok-free.app/api/v1/google-oauth/callback
NGROK_AUTHTOKEN=<secret>
```

## Shared-sheet mode

If new invoices must be inserted into one existing operator spreadsheet instead of creating a new spreadsheet per invoice, also set:

```env
GOOGLE_TARGET_SPREADSHEET_ID=<google_spreadsheet_id>
GOOGLE_TARGET_SHEET_NAME=Накладная
GOOGLE_TARGET_HEADER_ROW_COUNT=2
```

Behavior in this mode:

- backend writes into the existing sheet
- the newest invoice block is inserted directly below the header rows
- one empty separator row is inserted after each document block

## Local start

From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
python3 -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 --app-dir backend
```

Useful URLs:

```text
http://localhost:8000/ping
http://localhost:8000/docs
http://localhost:8000/api/v1/invoice-review/upload-page
```

## Docker start

From the repo root:

```bash
docker compose up --build
```

Notes:

- `docker-compose.yml` starts the backend in one command and publishes it on `localhost:8000`
- `.env` is loaded into the backend container
- `.env` is mounted at `/app/.env` so the OAuth callback can persist refreshed tokens
- SQLite data is stored in the Docker volume `autosnab_data`
- uploaded files are stored in the Docker volume `autosnab_uploads`
- CSV exports are stored in the Docker volume `autosnab_exports`
- MinerU/HuggingFace model cache is stored in the Docker volume `autosnab_hf_cache`
- no OAuth or service-account JSON is read by the active Google flow
- `backend/requirements.txt` installs the CPU PyTorch build and `mineru[pipeline]`, which is the backend selected by `MINERU_COMMAND`
- The first MinerU run downloads about 2.5 GB of model files into the user cache; later runs reuse that cache.
- if you want the Docker container to stay on OCR-only mode, keep `DOCUMENT_EXTRACTION_BACKEND=ocr`

Useful operational commands:

```bash
docker compose up --build -d
docker compose logs -f backend
docker compose down
```

To publish the local backend for a cloud `n8n` workflow:

```bash
docker compose --profile public-tunnel up -d ngrok
python3 scripts/get_ngrok_public_url.py
```

Notes:

- the `ngrok` profile publishes local backend traffic from `backend:8000`
- `scripts/get_ngrok_public_url.py` reads the local ngrok inspection API on `http://127.0.0.1:4040`
- use the printed HTTPS URL in cloud `n8n` as `Workflow Config -> backendBaseUrl`
- if the tunnel URL changes, update both `PUBLIC_API_BASE_URL` in `.env` and the same value in `n8n`
- if `.env` is not a usable file on this machine, start Compose with an explicit runtime file such as `BACKEND_ENV_FILE=.env.runtime`

## VPS deploy for BA/tester access (no purchased domain)

Use this instead of the ngrok profile when the backend needs to run on an
already-owned VPS with a stable public IP, independent of any developer's own
machine, so a business analyst can test the bot without depending on someone
leaving Docker running locally.

1. On the VPS: install Docker + the `docker compose` plugin, then get the repo
   onto the server (`git clone` or `rsync`).
2. Copy `.env` secrets over (`OPENAI_API_KEY`, `GOOGLE_OAUTH_*`,
   `GOOGLE_DRIVE_OCR_*`). An existing `GOOGLE_OAUTH_REFRESH_TOKEN` can be
   reused as-is — Google refresh tokens are not tied to the redirect URI, only
   the original authorization step was.
3. On a small/shared VPS (limited RAM, or already running other services),
   skip MinerU entirely: don't run `mineru.cli.models_download`, and leave
   `DOCUMENT_EXTRACTION_FALLBACK_TO_OCR=true`. `mineru_health()` reports
   unready with no model cache, and the pipeline already falls back to
   Google Drive OCR cleanly for that case — this avoids ever loading MinerU's
   CPU inference models (1GB+ RAM) into a box with little to spare. On a
   dedicated/larger box, optionally copy the MinerU model cache from the
   current machine's `autosnab_hf_cache` Docker volume to avoid
   re-downloading ~2.5 GB — but if you do download it fresh, **do not**
   `docker compose up --build` again while that download is in progress, or
   it can leave a corrupted partial model directory (hit and fixed once
   already; see `docs/wiki/log.md`, 2026-07-09 MinerU entry).
4. Find the VPS's public IPv4 address and turn it into a
   [nip.io](https://nip.io) hostname, e.g. `203.0.113.5` ->
   `203-0-113-5.nip.io` (dashes or dots both work; nip.io resolves either
   form straight back to the embedded IP — no DNS record to create).
5. Check whether port 443 is already in use on the VPS
   (`ss -tlnp | grep :443`) — a box already running another public-facing
   service (e.g. an existing VPN/proxy stack) commonly has it taken. If so,
   set `CADDY_HTTPS_HOST_PORT` to a free port instead (e.g. `8443`) and
   include it in every public URL below. Port 80 must stay free/mapped as-is
   either way — Let's Encrypt's HTTP-01 challenge always validates against
   port 80 specifically, independent of which port ends up serving traffic.
6. Set in `.env` on the VPS (add the `:PORT` suffix everywhere only if you
   set `CADDY_HTTPS_HOST_PORT` in the previous step):

```env
PUBLIC_DOMAIN=203-0-113-5.nip.io
CADDY_HTTPS_HOST_PORT=8443
PUBLIC_API_BASE_URL=https://203-0-113-5.nip.io:8443
GOOGLE_OAUTH_REDIRECT_URI=https://203-0-113-5.nip.io:8443/api/v1/google-oauth/callback
BOT_API_SHARED_SECRET=<generate a strong value — /bot/* is now genuinely public>
# Optional, recommended if the VPS is small or shared with other services:
BACKEND_MEM_LIMIT=700m
```

7. Open inbound port `80` and whichever port serves HTTPS (`443`, or your
   `CADDY_HTTPS_HOST_PORT` override) on the VPS firewall.
8. Start both the backend and the `caddy` reverse-proxy profile:

```bash
docker compose --profile public-ip up --build -d
```

9. Verify from *outside* the VPS (not `curl localhost`, an actual external
   client) that `https://203-0-113-5.nip.io:8443/health/runtime` returns
   `{"status": "ok", ...}` with a valid certificate, no `-k`/insecure flag
   needed.
10. In cloud `n8n`, set `Workflow Config -> backendBaseUrl` to the same
    public URL used above.
11. Re-run Google OAuth authorization only if the refresh token from step 2
    doesn't work (`/api/v1/google-oauth/status` will show it); otherwise skip.
12. Send the business analyst only the Telegram bot chat link — nothing else
    to install on her side.

Notes:

- Caddy's certificate is stored in the `autosnab_caddy_data` volume, so
  restarts do not re-request from Let's Encrypt and burn its rate limits.
- If the VPS's public IP ever changes (e.g. re-provisioned instance), repeat
  steps 4,6,9 with the new IP — nip.io needs no account or DNS changes either
  way, so this is a five-minute fix, not a redeploy.
- This profile replaces `public-tunnel`/ngrok for this deployment, not the
  local dev flow — a developer's own machine can still use `ngrok` for
  personal testing while the VPS serves the BA independently.

## Google OAuth setup

In Google Cloud:

1. Enable `Google Drive API`
2. Enable `Google Sheets API`
3. Create OAuth client type `Web application`
4. Add redirect URI:

```text
http://localhost:8000/api/v1/google-oauth/callback
```

If you are running Google OAuth through the public ngrok address instead of
localhost, register the public HTTPS callback URI instead:

```text
https://example.ngrok-free.app/api/v1/google-oauth/callback
```

Copy the client credentials from the downloaded JSON into `.env`:

```env
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
```

Then open:

```text
http://localhost:8000/api/v1/google-oauth/authorize
```

After successful login, the app fills these `.env` values:

```text
GOOGLE_OAUTH_ACCESS_TOKEN
GOOGLE_OAUTH_REFRESH_TOKEN
GOOGLE_OAUTH_TOKEN_EXPIRY
```

Quick status check:

```text
http://localhost:8000/api/v1/google-oauth/status
```

## Smoke test: invoice upload

### Browser flow

1. Open `http://localhost:8000/api/v1/invoice-review/upload-page`
2. Upload a JPG, PNG, or PDF invoice
3. Wait for OCR + parser + DB save
4. Confirm that the response shows either:
   - a Google Spreadsheet link
   - or a manual-review fallback with an OCR warning

### Curl flow

```bash
curl -X POST "http://localhost:8000/api/v1/invoice-review/upload-photo" \
  -F "file=@invoice.jpg" \
  -F "create_google_sheet=true" \
  -F "public_api_base_url=http://localhost:8000"
```

Expected response shape:

- `review_id`
- `status`
- `csv_path`
- `google_spreadsheet_id` / `google_spreadsheet_url` when Sheets succeeded
- `parser_provider`
- `parser_notes`

## Smoke test: shared-sheet prepend behavior

Use this only when `GOOGLE_TARGET_SPREADSHEET_ID` is configured.

1. Open the target spreadsheet
2. Confirm the target sheet exists, usually `Накладная`
3. Confirm the first `2` rows are the fixed header area if `GOOGLE_TARGET_HEADER_ROW_COUNT=2`
4. Upload one invoice
5. Check that:
   - the new document block starts directly under the header rows
   - old rows moved down
   - there is one empty row after the new document block
6. Upload a second invoice
7. Check that:
   - the second upload appears above the first one
   - document blocks are visually separated by an empty row

## Smoke test: send flow

1. Open the created review/send page:

```text
http://localhost:8000/api/v1/invoice-review/<review_id>/send-page
```

2. Verify that the backend can read the edited Google Sheet
3. Run send in dry/mock mode first with:

- `IIKO_INTEGRATION_ENABLED=false`

Expected result:

- payload is prepared and saved as an export record
- no real iiko call is required

## Local checks

Wiki checks:

```bash
python3 scripts/wiki_check.py
python3 scripts/raw_manifest_check.py
```

Python syntax spot-check:

```bash
python3 -m py_compile backend/app/config.py backend/app/services/google_sheets_service.py backend/app/services/invoice_review_service.py
```

Unit tests:

```bash
cd backend
python3 -m pytest
```

If `pytest` or other Python packages are missing, reinstall:

```bash
pip install -r backend/requirements.txt
```

## Known pitfalls

- if OAuth is not completed, Google OCR and Google Sheets creation will fail
- if the OAuth token is expired/revoked, re-run `/api/v1/google-oauth/authorize`
- if shared-sheet mode is enabled but the target sheet name is wrong, sheet writing will fail
- if the target spreadsheet is not shared with the authorized Google user, API writes will fail
- if dev dependencies are missing from the shell, `pytest` will not run even when the app code itself is fine

## Current MVP focus

The current priority is not the whole product surface. The runbook is optimized for the downloaded-invoice -> validation-table MVP.
