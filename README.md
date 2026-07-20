# АвтоСнаб MVP

`autosnab_mvp` сейчас решает одну ближайшую задачу: провести накладную от загрузки файла до проверочной таблицы и подготовить данные к отправке в iiko.

Это не общий "документооборот" и не финальная procurement-платформа. Текущий центр продукта: `upload -> extract -> normalize -> validate in Google Sheets -> send/mock iiko`.

## Текущий фокус проекта

- Основной MVP: надежно загружать фото/скан/PDF накладной и класть результат в существующую таблицу проверки.
- Ближайшие риски: многополосные документы, стабильность распознавания, сохранение реального табличного контракта `Накладная`, ручная дозаправка справочников.
- Следующий этап: SBIS EDO как еще один source adapter поверх того же document core.
- Более дальний трек: supplier catalog, price ingestion и поиск по товарам.

## Что реально работает

### 1. Загрузка и распознавание накладной

Пользовательский вход:

- `GET /api/v1/invoice-review/upload-page`

После загрузки файла backend:

1. сохраняет исходник в `uploads/invoices`
2. запускает extraction backend
3. нормализует результат встроенным парсером
4. создает запись проверки в SQLite
5. сохраняет CSV-экспорт
6. при включенном Google Sheets создает отдельную таблицу или пишет в общую рабочую таблицу

Поддерживаемые входы:

- `JPG`
- `PNG`
- `PDF`

### 2. Два extraction backend'а

По умолчанию используется:

- `DOCUMENT_EXTRACTION_BACKEND=ocr`

Это Google Drive OCR через OAuth пользователя.

Опционально можно включить:

- `DOCUMENT_EXTRACTION_BACKEND=mineru`

Тогда backend вызывает локальный MinerU по контракту:

```bash
python -m mineru.cli.client \
  -p <input_path> \
  -o <output_path> \
  -b pipeline \
  -l cyrillic
```

Для CPU-окружения зависимости устанавливаются из `backend/requirements.txt`.
Первый запуск дополнительно скачивает около 2.5 ГБ моделей MinerU; следующие
запуски используют локальный кэш.

Если MinerU включен, но не справился, можно оставить fallback:

- `DOCUMENT_EXTRACTION_FALLBACK_TO_OCR=true`

### 3. OpenAI structured parser и ручной fallback

Сейчас pipeline такой:

```text
invoice file
-> PDF text / Google Drive OCR / MinerU evidence
-> OpenAI strict structured output
-> deterministic normalization and status rules
-> invoice review payload
-> deterministic Google Sheets mapper
```

OpenAI не выбирает колонки и не пишет в Google Sheets. Для запуска задайте
`OPENAI_API_KEY`; модель по умолчанию настраивается через
`OPENAI_INVOICE_MODEL=gpt-5-mini`.

### 4. Google Sheets как рабочий интерфейс оператора

Проект сейчас ориентирован на Google Sheets как на основную поверхность ручной проверки.

Есть два режима:

- создание отдельной таблицы под review
- запись в уже существующую таблицу заведения через `GOOGLE_TARGET_SPREADSHEET_ID`

Для shared-sheet режима важны настройки:

```env
GOOGLE_TARGET_SPREADSHEET_ID=
GOOGLE_TARGET_SHEET_NAME=Накладная
GOOGLE_TARGET_HEADER_ROW_COUNT=2
```

Поведение в shared-sheet режиме:

- новые строки вставляются сразу под шапкой
- старые данные сдвигаются вниз
- после блока документа добавляется одна пустая строка

### 5. Отправка в iiko

После ручной проверки пользователь открывает:

- `GET /api/v1/invoice-review/{review_id}/send-page`

Дальше backend читает актуальные данные из Google Sheets и либо:

- собирает dry-run/mock export
- либо пытается отправить payload в iiko

Ключевой endpoint:

- `POST /api/v1/invoice-review/{review_id}/send-from-google-sheet`

## Архитектурная рамка

Проект уже нужно читать не как один hardcoded flow, а как общий document core с адаптерами:

- photo/PDF upload: текущий активный source adapter
- Google Drive OCR: extraction backend по умолчанию
- MinerU: локальный extraction backend
- SBIS EDO: следующий source adapter
- iiko: downstream integration target

Правило для дальнейшей разработки: не плодить отдельные пайплайны под каждый источник, а держать единый контракт документа, нормализации, статусов и выгрузки.

## Основные сценарии API

### Сервис

- `GET /ping`
- `GET /docs`

### OAuth Google

- `GET /api/v1/google-oauth/status`
- `GET /api/v1/google-oauth/authorize`
- `GET /api/v1/google-oauth/callback`
- `POST /api/v1/google-oauth/logout`

### Invoice review MVP

- `GET /api/v1/invoice-review/upload-page`
- `POST /api/v1/invoice-review/upload-photo`
- `POST /api/v1/invoice-review/upload`
- `PUT /api/v1/invoice-review/{review_id}`
- `GET /api/v1/invoice-review/iiko/references/status`
- `POST /api/v1/invoice-review/{review_id}/iiko-auto-map`
- `GET /api/v1/invoice-review/{review_id}/sheet`
- `GET /api/v1/invoice-review/{review_id}/sheet.csv`
- `GET /api/v1/invoice-review/{review_id}/preview`
- `GET /api/v1/invoice-review/{review_id}/apps-script`
- `POST /api/v1/invoice-review/{review_id}/google-sheet`
- `GET /api/v1/invoice-review/{review_id}/send-page`
- `POST /api/v1/invoice-review/{review_id}/send-from-google-sheet`
- `POST /api/v1/invoice-review/{review_id}/sync-sheet-and-confirm-send`
- `POST /api/v1/invoice-review/{review_id}/confirm-send`
- `GET /api/v1/invoice-review/exports/iiko`

### Receiving / accounting MVP

Это отдельный, более старый API-слой приемки и сверки. Он остается в проекте, но текущий продуктовый центр не здесь.

- `POST /api/v1/receiving/start`
- `POST /api/v1/receiving/{receiving_id}/documents`
- `POST /api/v1/receiving/{receiving_id}/compare-invoice`
- `POST /api/v1/receiving/{receiving_id}/corrections`
- `POST /api/v1/receiving/{receiving_id}/corrections/parse`
- `POST /api/v1/receiving/{receiving_id}/corrections/text`
- `POST /api/v1/receiving/{receiving_id}/confirm`
- `GET /api/v1/receiving/{receiving_id}`
- `GET /api/v1/receiving/{receiving_id}/accounting-payload`
- `POST /api/v1/receiving/export/google-sheets-mvp`
- `POST /api/v1/accounting/mappings`
- `GET /api/v1/accounting/mappings`
- `POST /api/v1/accounting/receivings/{receiving_id}/send`
- `GET /api/v1/accounting/exports`

### Backoffice / diagnostics

- `GET /api/v1/receiving/{receiving_id}/documents`
- `GET /api/v1/documents/history`
- `GET /api/v1/documents/{document_id}`
- `GET /api/v1/documents/{document_id}/view`
- `GET /api/v1/analytics/discrepancies`
- `GET /api/v1/suppliers/control`
- `GET /api/v1/iiko/receivings/{receiving_id}/payload`
- `POST /api/v1/iiko/receivings/{receiving_id}/send`
- `GET /api/v1/iiko/exports`

## Локальный запуск

### 1. Установить зависимости

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

### 2. Подготовить `.env`

Базовый вариант:

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
GOOGLE_INVOICE_REGISTER_SPREADSHEET_ID=
GOOGLE_INVOICE_REGISTER_SPREADSHEET_URL=
PUBLIC_API_BASE_URL=http://localhost:8000
UPLOADED_INVOICES_DIR=uploads/invoices

DOCUMENT_EXTRACTION_BACKEND=ocr
DOCUMENT_EXTRACTION_FALLBACK_TO_OCR=true
MINERU_COMMAND={python_executable} -m mineru.cli.client -p {file_path} -o {output_dir} -b pipeline -l cyrillic
MINERU_TIMEOUT_SECONDS=900

IIKO_INTEGRATION_ENABLED=false
IIKO_BASE_URL=
IIKO_LOGIN=
IIKO_PASSWORD_SHA1=
IIKO_TOKEN=
IIKO_TIMEOUT_SECONDS=30
IIKO_AUTO_MAPPING_ENABLED=true
IIKO_MAPPING_MIN_CONFIDENCE=0.72
```

Для облачного `n8n` вместо `localhost` нужен один и тот же публичный HTTPS base URL backend, например `https://example.ngrok-free.app`:

- `PUBLIC_API_BASE_URL=https://example.ngrok-free.app`
- `GOOGLE_OAUTH_REDIRECT_URI=https://example.ngrok-free.app/api/v1/google-oauth/callback`

Актуальный шаблон также лежит в [.env.example](/home/viktor-shadrin/PycharmProjects/autosnab_mvp/.env.example).

### 3. Пройти OAuth

В Google Cloud нужны:

- `Google Drive API`
- `Google Sheets API`
- OAuth client типа `Web application`

Redirect URI:

```text
http://localhost:8000/api/v1/google-oauth/callback
```

После старта backend откройте:

```text
http://localhost:8000/api/v1/google-oauth/authorize
```

После callback приложение само запишет в `.env`:

- `GOOGLE_OAUTH_ACCESS_TOKEN`
- `GOOGLE_OAUTH_REFRESH_TOKEN`
- `GOOGLE_OAUTH_TOKEN_EXPIRY`

Если OAuth уже был успешно пройден локально и в `.env` сохранён рабочий refresh token, для одной только интеграции бота с облачным `n8n` переавторизация через публичный callback обычно не требуется. Но при следующем полном OAuth-цикле redirect URI должен совпадать с публичным URL backend.

### 4. Запустить backend

```bash
python3 -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 --app-dir backend
```

Точки входа:

- `http://localhost:8000/ping`
- `http://localhost:8000/docs`
- `http://localhost:8000/api/v1/invoice-review/upload-page`

## Docker

После подготовки `.env` сервис поднимается одной командой:

```bash
docker compose up --build
```

Что делает текущая Docker-конфигурация:

- поднимает один контейнер FastAPI на `http://localhost:8000`
- читает переменные из локального `.env`
- монтирует `.env` в `/app/.env`, чтобы OAuth callback мог обновлять токены прямо в этот файл
- хранит SQLite, `uploads`, `exports` и HuggingFace/MinerU cache в named volumes Docker
- не тянет в build context `.venv`, локальные базы, `uploads`, `exports` и прочие тяжелые артефакты

Полезные команды:

```bash
docker compose up --build -d
docker compose logs -f backend
docker compose down
```

## Telegram-бот через облачный n8n

Бот — это тонкий, полностью stateless Telegram-роутер поверх того же backend:
он не хранит никакого состояния сам (ни сессий, ни файлов); всё состояние
"какой документ сейчас собирается" живёт в backend (`ingestion_uploads`,
статус `collecting`) и адресуется по `chat_id`. Логика распознавания,
нормализации и записи в Google Sheets не дублируется — бот вызывает те же
эндпоинты, что и веб-загрузка.

Подробный архитектурный разбор: [docs/wiki/telegram-bot-cloud-n8n-plan.md](docs/wiki/telegram-bot-cloud-n8n-plan.md).
Контракт эндпоинтов бота: [docs/wiki/bot-backend-api-contract.md](docs/wiki/bot-backend-api-contract.md).
Разбор по узлам воркфлоу и известные особенности схемы n8n: [n8n/telegram-bot-node-setup.md](n8n/telegram-bot-node-setup.md).

Ниже — самодостаточная пошаговая инструкция, чтобы поднять бота с нуля.

### Что подготовить заранее

| Что нужно | Где взять |
|---|---|
| Docker + Docker Compose | уже должен быть установлен для локального запуска backend |
| Аккаунт ngrok + authtoken | зарегистрироваться на https://ngrok.com, токен лежит на https://dashboard.ngrok.com/get-started/your-authtoken |
| Токен Telegram-бота | создать бота через [@BotFather](https://t.me/BotFather) командой `/newbot`, скопировать выданный токен вида `123456:ABC-DEF...` |
| Аккаунт в облачном n8n | https://n8n.cloud (или любой другой n8n с публичным доступом для приёма вебхуков от Telegram) |
| Ключ OpenAI | https://platform.openai.com/api-keys — нужен для структурного парсинга накладных |
| Доступ к Google Cloud проекту | для Google Drive OCR + Google Sheets, см. раздел "Пройти OAuth" выше |

### Шаг 1. Заполнить `.env` backend'а

Скопируйте `.env.example` в `.env` и заполните минимум:

```env
# базовые ключи — как в разделе "Локальный запуск" выше
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
OPENAI_API_KEY=...
GOOGLE_TARGET_SPREADSHEET_ID=...        # ID таблицы из её URL

# специфично для бота через облачный n8n
NGROK_AUTHTOKEN=<токен из ngrok dashboard>
BOT_API_SHARED_SECRET=<длинная случайная строка>
PUBLIC_API_BASE_URL=http://localhost:8000   # обновится на шаге 3
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/api/v1/google-oauth/callback  # тоже обновится на шаге 3
```

`BOT_API_SHARED_SECRET` — это не логин/пароль откуда-то, а просто секрет,
который вы сами придумываете один раз и используете в двух местах (backend
`.env` и n8n credential на шаге 6). Сгенерировать можно так:

```bash
openssl rand -hex 32
```

Пока `BOT_API_SHARED_SECRET` пуст, backend не проверяет заголовок
`X-Bot-Api-Key` — это ок для локальной разработки без публичного туннеля, но
**обязательно** заполните его перед тем, как открывать backend наружу через
`ngrok`, иначе загрузку документов сможет вызвать кто угодно, кто узнает URL.

### Шаг 2. Поднять backend вместе с публичным туннелем

```bash
docker compose --profile public-tunnel up --build -d
```

Это поднимает два контейнера: `backend` (FastAPI на `:8000`) и `ngrok`
(публикует его наружу). Если путь `.env` на машине занят или нужен отдельный
runtime-файл:

```bash
cp .env.example .env.runtime
BACKEND_ENV_FILE=.env.runtime docker compose --profile public-tunnel up --build -d
```

Проверить, что оба контейнера здоровы:

```bash
docker compose ps
docker compose logs -f backend
```

### Шаг 3. Получить публичный URL и вписать его обратно в `.env`

```bash
python3 scripts/get_ngrok_public_url.py
```

Выведет что-то вроде `https://example.ngrok-free.app`. Впишите это значение в
`.env` (или `.env.runtime`) вместо `localhost`:

```env
PUBLIC_API_BASE_URL=https://example.ngrok-free.app
GOOGLE_OAUTH_REDIRECT_URI=https://example.ngrok-free.app/api/v1/google-oauth/callback
```

и перезапустите backend, чтобы значения подхватились:

```bash
docker compose --profile public-tunnel up --build -d
```

Проверка, что backend действительно отвечает через туннель:

```bash
curl "$(python3 scripts/get_ngrok_public_url.py)/health/runtime"
```

**Важно про free-tier ngrok:** этот URL меняется при каждом перезапуске
контейнера `ngrok`. После рестарта нужно будет заново выполнить этот шаг и
обновить только узел `Workflow Config` в n8n (шаг 7) — остальной воркфлоу
трогать не нужно. Если рестарты частые и это раздражает — у ngrok есть
платный вариант с закреплённым доменом.

### Шаг 4. Пройти Google OAuth (если ещё не проходили)

Откройте `https://<ваш-ngrok-url>/api/v1/google-oauth/authorize`, войдите
нужным Google-аккаунтом. После успешного callback backend сам запишет
`GOOGLE_OAUTH_ACCESS_TOKEN` / `GOOGLE_OAUTH_REFRESH_TOKEN` /
`GOOGLE_OAUTH_TOKEN_EXPIRY` обратно в `.env`. Если это уже было сделано
локально и refresh token рабочий — повторно проходить не обязательно.

### Шаг 5. Импортировать воркфлоу в облачный n8n

В n8n: **Workflows → Import from File** → выбрать
`n8n/telegram-bot-mvp.workflow.json` из этого репозитория. Импортируется как
неактивный черновик.

### Шаг 6. Создать в n8n два credential'а

Воркфлоу ссылается на два credential'а по имени; после импорта на
соответствующих узлах будет видно "Credential not found", пока вы их не
создадите и не привяжете:

**Telegram API** — назовите как угодно, например `Telegram account`:
- Access Token: токен бота от `@BotFather` (см. таблицу выше)
- Привязать к узлам: `Telegram Trigger`, `Get Telegram File`, `Send Reply`,
  `Send Stage Update`

**HTTP Header Auth** — назовите как угодно, например `Backend URL`:
- Name: `X-Bot-Api-Key`
- Value: то же значение, что и `BOT_API_SHARED_SECRET` в `.env`
- Привязать ко всем `HTTP Request`-узлам: `Send Page To Backend`,
  `Finalize Draft`, `Check Upload Status`, `Check Draft Status`,
  `Check Latest Upload`, `Reset Draft`

### Шаг 7. Настроить узел `Workflow Config`

Откройте первый `Code`-узел после триггера — `Workflow Config` — и замените
`backendBaseUrl` на URL из шага 3:

```js
backendBaseUrl: 'https://example.ngrok-free.app',
```

Это единственное место, где захардкожен адрес backend — всё остальное
обращается к нему через
`{{ $('Workflow Config').item.json.backendBaseUrl }}`.

### Шаг 8. Активировать и проверить

Включите воркфлоу (тумблер вверху справа) — n8n сам зарегистрирует Telegram
webhook. Откройте чат с ботом и проверьте:

1. Одна страница накладной → `Готово` → итог с поставщиком/суммой/ссылкой на таблицу.
2. Две страницы одной накладной → `Готово` → один документ, а не два.
3. Повторная отправка того же документа → пометка про дубликат.
4. Неподдерживаемый формат файла → понятный отказ, черновик не портится.
5. `Статус` во время сбора страниц → корректное число страниц.
6. `Сбросить` → следующая страница начинает новый черновик.
7. Остановить backend во время обработки → `Статус` всё равно отдаёт
   последнее известное состояние, а не ошибку узла n8n.

### Частые проблемы

- **`Could not find property option` при импорте JSON** — обычно значит, что
  в файл руками добавили параметр узла, которого нет в установленной версии
  ноды (типичный кандидат — клавиатура Telegram-сообщения). Самый надёжный
  способ настроить клавиатуру — сделать это через UI узла `Send Reply`
  (Additional Fields → Reply Markup → Reply Keyboard), а не редактировать
  JSON руками; UI не даст сохранить невалидную схему.
- **401 от backend** — проверьте, что значение credential'а `X-Bot-Api-Key`
  в n8n совпадает с `BOT_API_SHARED_SECRET` в `.env` backend'а.
- **Бот не отвечает вообще** — проверьте `docker compose ps` (оба контейнера
  должны быть `healthy`/`Up`), затем `python3 scripts/get_ngrok_public_url.py`
  (туннель должен отдавать текущий URL), затем что именно этот URL стоит в
  узле `Workflow Config`.
- **После перезапуска `ngrok` бот снова не отвечает** — почти всегда это
  просто новый URL; повторите шаг 3 и обновите только `Workflow Config`.

## Структура кода

Ключевые точки:

- [backend/app/main.py](/home/viktor-shadrin/PycharmProjects/autosnab_mvp/backend/app/main.py)
- [backend/app/config.py](/home/viktor-shadrin/PycharmProjects/autosnab_mvp/backend/app/config.py)
- [backend/app/routers/invoice_review.py](/home/viktor-shadrin/PycharmProjects/autosnab_mvp/backend/app/routers/invoice_review.py)
- [backend/app/routers/google_oauth.py](/home/viktor-shadrin/PycharmProjects/autosnab_mvp/backend/app/routers/google_oauth.py)
- [backend/app/routers/receiving.py](/home/viktor-shadrin/PycharmProjects/autosnab_mvp/backend/app/routers/receiving.py)
- [backend/app/services/document_extraction_service.py](/home/viktor-shadrin/PycharmProjects/autosnab_mvp/backend/app/services/document_extraction_service.py)
- [backend/app/services/ocr_service.py](/home/viktor-shadrin/PycharmProjects/autosnab_mvp/backend/app/services/ocr_service.py)
- [backend/app/services/google_sheets_service.py](/home/viktor-shadrin/PycharmProjects/autosnab_mvp/backend/app/services/google_sheets_service.py)
- [backend/app/services/google_oauth_service.py](/home/viktor-shadrin/PycharmProjects/autosnab_mvp/backend/app/services/google_oauth_service.py)
- [backend/app/services/invoice_review_service.py](/home/viktor-shadrin/PycharmProjects/autosnab_mvp/backend/app/services/invoice_review_service.py)
- [backend/app/services/iiko_incoming_invoice_service.py](/home/viktor-shadrin/PycharmProjects/autosnab_mvp/backend/app/services/iiko_incoming_invoice_service.py)


## Проверки

Wiki:

```bash
python3 scripts/wiki_check.py
python3 scripts/raw_manifest_check.py
```

Быстрый Python spot-check:

```bash
python3 -m py_compile backend/app/config.py backend/app/services/google_sheets_service.py backend/app/services/invoice_review_service.py
```

Точечные тесты:

```bash
pytest backend/tests/test_google_oauth_service.py
pytest backend/tests/test_document_extraction_service.py
pytest backend/tests/test_google_sheets_service.py
```

## Актуальные ограничения

- Shared-sheet запись уже переписана под реальный табличный контракт `Накладная`, но нужен живой ретест на пользовательской Google-таблице.
- Полный `pytest` сейчас не считается полностью надежным smoke-check: ранее зависал на `backend/tests/test_receiving.py::test_start_receiving`.
- OAuth credentials уже были в Git history до миграции в `.env`, поэтому их нужно перевыпустить и отозвать старые.

## Правила работы с репозиторием

- Секреты не хранятся в Git, только в локальном `.env`.
- Новые не-кодовые исходники сначала регистрируются в `manifests/raw_sources.csv`, потом используются.
- Для любых заметных изменений нужен wiki writeback в `docs/wiki/`.
