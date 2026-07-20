# Telegram Bot MVP — Cloud n8n Setup Guide

Companion to `n8n/telegram-bot-mvp.workflow.json` and
`docs/wiki/telegram-bot-cloud-n8n-plan.md`. This workflow is a stateless
Telegram router: it holds no session/draft data itself — everything about
"which document is this page part of" lives in the backend
(`ingestion_uploads`, `collecting` status), keyed by `chat_id`.

This file is now the user's own confirmed-working cloud-n8n export (real
credential IDs, real `webhookId`s, real `backendBaseUrl`), with incremental
fixes applied on top by re-editing that export rather than hand-authoring a
fresh one. Anything described below as "hand-authored and unverified" refers
only to the parts that haven't gone through a live import yet — most of the
workflow already has.

## 1. Prerequisites

- Backend running locally via `docker compose --profile public-tunnel up --build`
  (see `docker-compose.yml`), with `NGROK_AUTHTOKEN` and a strong
  `BOT_API_SHARED_SECRET` set in `.env`.
- Current public URL: `python3 scripts/get_ngrok_public_url.py`.
- A Telegram bot token from `@BotFather`.

## 2. Import

In cloud n8n: **Workflows → Import from File** → select
`telegram-bot-mvp.workflow.json`. It imports as an inactive draft named
`Autosnab Telegram Bot MVP (Cloud n8n)`.

## 3. Credentials already wired

This export already references the two real credentials from the user's
n8n instance:

- **Telegram account** (Telegram API) — attached to **Telegram Trigger**,
  **Get Telegram File**, **Send Reply**, **Send Stage Update**.
- **Backend URL** (HTTP Header Auth, header `X-Bot-Api-Key`) — attached to
  every `HTTP Request` node: **Send Page To Backend**, **Finalize Draft**,
  **Check Upload Status**, **Check Draft Status**, **Check Latest Upload**,
  **Reset Draft**.

If you ever re-import this file into a *different* n8n instance, those
credential IDs won't resolve there — recreate credentials with the same
names and reattach them to the same node list above.

## 4. Workflow Config node

The **Workflow Config** node's `backendBaseUrl` is pre-filled with the
current `ngrok` URL. This is the **only** place the backend URL is
hardcoded — everything else references it via
`{{ $('Workflow Config').item.json.backendBaseUrl }}`. When `ngrok` restarts
with a new URL (free tier), this is the only node you need to edit.

`maxPollAttempts` (default `24`, ~2 minutes at 5s/attempt) controls how long
the bot waits for processing to finish before telling the user to check
`Статус` later — raise it if OpenAI/MinerU runs are consistently slower than
that on your machine.

## 4a. Attribution footer disabled, and a dedicated "started" reply node

Every outgoing Telegram send node (`Send Reply`, `Send Stage Update`, and the
new `Send Started Reply`) now sets `additionalFields.appendAttribution: false`,
which removes the `This message was sent automatically with n8n` footer n8n
adds by default.

`Reply Processing Started` ("Принял, обрабатываю документ...") used to fan out
in parallel to both `Send Reply` and `Prepare Poll`. In practice this let the
poll-loop branch (which includes a `Wait` node) finish and send the *final*
result before the "started" message actually reached Telegram, so operators
saw the processing message arrive *after* the result — confusing. Fixed by
giving that one reply its own dedicated node, `Send Started Reply` (same
config as `Send Reply`, including the keyboard), wired strictly in sequence:
`Reply Processing Started` → `Send Started Reply` → `Prepare Poll`. This
guarantees the "started" message is actually sent before the poll loop
begins, since `Prepare Poll` now depends on that node's output instead of
racing it. `Send Reply` itself is unchanged and still used by every other
reply path.

## 4b. Publish before testing — editor changes are not live until published

This cloud-n8n workspace has a Draft/Publish split (see the **Publish**
button, top-right of the editor). Importing or editing the workflow only
updates the *draft* graph — the live Telegram-triggered execution keeps
running whatever was last **published**, even if the editor visibly shows
the new nodes. Confirmed on 2026-07-09: after adding the `Send Started
Reply` node fix, a live test still showed the old bug (attribution footer
present, "Принял, обрабатываю..." arriving last), and the Executions tab
for that exact run showed the *old* graph (no `Send Started Reply` node)
even though the editor already had it. Clicking **Publish** and re-testing
fixed both. Always publish after importing/editing before testing live —
if a fix "doesn't work," check the Executions tab's graph view for that
run before re-diagnosing the workflow JSON itself.

## 5. Notes on a few node shapes

- **Send Page To Backend** (multipart body): the `file` field is a Body
  Parameter of type *n8n Binary File*, Input Data Field Name `data` — this
  uploads whatever **Get Telegram File** downloaded into its `data` binary
  property.
- **Finalize Draft** / **Check Latest Upload**: both use *On Error → Continue
  (using error output)*, giving them two outputs (success / error) instead of
  a separate `IF` node.
- Any `IF` node: each has exactly one condition, written as a full boolean
  expression compared to `true` (e.g. `{{$json.intent === 'file'}}` equals
  `true`), rather than n8n's built-in string/exists operators. Deliberate
  choice for stability across n8n versions — no need to change it.
- **Send Reply** (reply keyboard) — confirmed-working schema, learned from
  this live export: `replyMarkup: "replyKeyboard"` and `replyKeyboard.rows`
  is an array where each row is `{ "row": { "buttons": [{ "text": "..." }] } }`
  (**`buttons`**, not `values` — an earlier hand-authored guess used `values`
  and broke import with `Could not find property option`; this is the
  corrected, confirmed shape). Two rows: `Готово`/`Статус`, then `Сбросить`.

## 6. Progress messages while a document is processing

So the operator never wonders if the bot "hung", the poll loop
(`Prepare Poll → Wait Before Poll → Check Upload Status → If Poll Done →
Compute Stage → If Stage Changed → Set Stage Reply Text → Send Stage
Update`) sends one extra Telegram message every time the backend's
`pipeline_logs` move into a new coarse stage, in this order:

1. `Документ принят в обработку.` — sent immediately after `Готово`
   (via **Reply Processing Started**, before the poll loop even starts).
2. `Выгружаем данные из скана...` — once OCR/MinerU/image-prep stages start.
3. `Обрабатываем данные через ИИ...` — once the OpenAI/reference-mapping
   stages start.
4. `Загружаем данные в таблицу...` — once the Google Sheets write starts.
5. The final result message (supplier/invoice/sum/sheet link, or the
   duplicate/error/needs-review text) once `completed` is `true`.

**Compute Stage** derives the current coarse stage from the *last* entry in
`pipeline_logs` and only lets a message through when the stage actually
changed since the last one sent (tracked via `Set Stage Reply Text`'s output,
read back with `$('Set Stage Reply Text')` — the standard n8n trick for
carrying a value across loop iterations without any external session
storage). If processing is fast enough that a stage is skipped between two
5-second polls, that message is simply never sent — this is expected, not a
bug.

The reply keyboard is only attached to **Send Reply** (the final-answer
node); it doesn't need to be repeated on **Send Stage Update**, since a
Telegram reply keyboard stays visible across messages once shown once.

## 7. "Статус" after "Сбросить" shows an old document — intentional, now labeled

If the operator presses `Сбросить` and then `Статус` with no new pages sent,
there is no open draft, so the bot falls back to the last *finished* upload
for that chat (`GET /bot/uploads/latest`) — which can be an older document
from before the reset. This looked like a bug in testing because the reply
read like it was about the just-cleared draft. **Mark As Status Command**
now tags that specific branch (`Check Latest Upload` → success →
`Mark As Status Command` → `Format Upload Result Message`), and the message
is prefixed with `Активного черновика нет. Последний обработанный документ:`
so it reads as history, not as a live answer to the reset.

## 8. Activate and test

Activate the workflow (top-right toggle), set the Telegram webhook if not
done automatically by the trigger node, then run the smoke tests from
`docs/wiki/telegram-bot-cloud-n8n-plan.md`:

1. Single photo → `Готово` → result with summary + sheet link.
2. Two photos → `Готово` → one logical document, not two.
3. Re-send the same document → duplicate result.
4. Unsupported file → clear rejection, draft untouched.
5. `Статус` mid-collection → correct page count.
6. `Сбросить` mid-collection → next file starts a fresh draft.
7. Stop the Docker backend mid-processing → `Статус` still returns the last
   durable state instead of a workflow error.
8. Restart `ngrok` → update only `Workflow Config` → bot works again.
