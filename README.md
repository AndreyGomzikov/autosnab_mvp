# АвтоСнаб — Backend MVP-4

Backend-проект для сценария загрузки накладной, распознавания данных, ручной проверки в Google Таблице и последующей подготовки/отправки данных в iiko.

Основной текущий сценарий:

```text
Фото / PDF накладной
↓
загрузка через web-страницу backend
↓
Google Drive OCR распознает текст документа
↓
встроенный regex parser структурирует OCR-текст в JSON
↓
если данные не найдены — создается пустая таблица для ручной проверки
↓
backend формирует Google Таблицу «АвтоСнаб Накладные»
↓
пользователь проверяет и исправляет строки в Google Таблице
↓
страница отправки читает фактические данные из Google Таблицы
↓
backend формирует payload / XML для iiko
↓
ручное подтверждение отправки в iiko
```

## Что сейчас реализовано

### 1. Загрузка накладной через браузер

Основная пользовательская страница:

```text
GET /api/v1/invoice-review/upload-page
```

На странице можно выбрать фото, скан или PDF накладной. После загрузки backend:

- сохраняет файл в папку `uploads/invoices`;
- запускает Google Drive OCR;
- передает распознанный текст во встроенный regex parser;
- создает запись проверки накладной в базе;
- создает CSV-копию в `exports/invoice_review_{review_id}.csv`;
- при включенном `GOOGLE_SHEETS_ENABLED=true` создает Google Таблицу для проверки.

### 2. Google Таблица для проверки накладных

Текущий формат Google Таблицы — один основной лист:

```text
Накладные
```

Название создаваемой таблицы:

```text
АвтоСнаб Накладные
```

Диапазон записи в Google Sheets:

```text
A1:AL500
```

В таблице сейчас 38 колонок. Актуальная структура заголовков:

```text
Время загрузки документа
ID документа
Индикатор дубля документа
Форма документа
Дата документа
№ Документа
Поставщик
ИНН Поставщика
Грузополучатель
Получатель
Торговая точка
Склад
Основание
Наименование товара из документа
Госсистемы
Наименование товара в УС
Товар найден в справочнике
Ед.изм.
Кол-во из документа
Цена за единицу
Стоимость без НДС
Ставка НДС %
Сумма НДС
Общая стоимость
Сумма накладной
Ед.изм. в УС
Кол-во в УС
Цена в УС
Дата приема
Принял, Ф.И.О.
Кол-во в заявке
Цена по прайсу
Последняя дата поставки
Последняя цена
Отклонение от цены прайса
Загрузить в УС
Статус строки
Причина ручной корректировки
```

Важно: отдельных столбцов `ЕГАИС`, `Меркурий`, `Честный знак` больше нет. Вместо них используется один общий столбец `Госсистемы`. Предпоследний столбец называется `Статус строки`.

### 3. Google OAuth вместо service account

Текущий основной режим работы с Google Drive OCR и Google Sheets — OAuth обычного Google-аккаунта.

Используются файлы:

```text
backend/secrets/oauth-client.json
backend/secrets/oauth-token.json
```

`oauth-client.json` нужно скачать из Google Cloud Console как OAuth Client ID типа `Web application`.

`oauth-token.json` создается автоматически после авторизации через endpoint:

```text
GET /api/v1/google-oauth/authorize
```

Проверить статус авторизации можно здесь:

```text
GET /api/v1/google-oauth/status
```

Выйти из Google-авторизации локально:

```text
POST /api/v1/google-oauth/logout
```

### 4. OCR через Google Drive OCR

Google Vision API больше не используется.

Для OCR нужны:

- Google Drive API;
- Google Sheets API;
- OAuth Client ID;
- авторизация пользователя через `/api/v1/google-oauth/authorize`.

Логика OCR:

```text
JPG / PNG / PDF
↓
временный Google Docs документ через Google Drive API
↓
экспорт распознанного текста в text/plain
↓
удаление временного документа, если GOOGLE_DRIVE_OCR_DELETE_TEMP_FILES=true
```

### 5. Парсер накладной и fallback

Файл текущего парсера OCR-текста:

```text
backend/app/services/invoice_parser_service.py
```

Каскад обработки:

```text
1. встроенный regex parser
2. manual_review_empty_sheet
```

В текущей версии внешний AI parser не используется. Проект не требует внешних API-ключей для разбора накладной. Если regex parser не нашел полезные поля, backend создает пустую таблицу для ручной проверки.

### 6. Отправка в iiko

После проверки таблицы пользователь открывает страницу отправки:

```text
GET /api/v1/invoice-review/{review_id}/send-page
```

На этой странице backend читает актуальные данные из листа `Накладные` и вызывает:

```text
POST /api/v1/invoice-review/{review_id}/send-from-google-sheet
```

Также есть прямой endpoint для ручного подтверждения:

```text
POST /api/v1/invoice-review/{review_id}/confirm-send
```

И endpoint для синхронизации данных из таблицы с последующим подтверждением:

```text
POST /api/v1/invoice-review/{review_id}/sync-sheet-and-confirm-send
```

Отправка в iiko поддерживает два режима:

- `IIKO_INTEGRATION_ENABLED=false` — mock/export режим, payload сохраняется в базе;
- `IIKO_INTEGRATION_ENABLED=true` — попытка реальной отправки через iiko Server API при заполненных настройках iiko.

## Основные файлы проекта

```text
backend/app/main.py
backend/app/config.py
backend/app/routers/invoice_review.py
backend/app/routers/google_oauth.py
backend/app/routers/receiving.py
backend/app/routers/receiving_backoffice.py
backend/app/routers/accounting.py
backend/app/services/invoice_review_service.py
backend/app/services/google_sheets_service.py
backend/app/services/google_oauth_service.py
backend/app/services/ocr_service.py
backend/app/services/invoice_parser_service.py
backend/app/services/iiko_incoming_invoice_service.py
backend/app/services/iiko_reference_mapping_service.py
backend/app/models/receiving.py
backend/app/models/accounting.py
backend/tests/test_receiving.py
backend/tests/test_ocr_parser.py
```

## Endpoint'ы

### Сервис

```text
GET /ping
GET /docs
```

### Google OAuth

```text
GET  /api/v1/google-oauth/status
GET  /api/v1/google-oauth/authorize
GET  /api/v1/google-oauth/callback
POST /api/v1/google-oauth/logout
```

### Проверка накладной

```text
GET  /api/v1/invoice-review/upload-page
POST /api/v1/invoice-review/upload-photo
POST /api/v1/invoice-review/upload
PUT  /api/v1/invoice-review/{review_id}
GET  /api/v1/invoice-review/iiko/references/status
POST /api/v1/invoice-review/{review_id}/iiko-auto-map
GET  /api/v1/invoice-review/{review_id}/sheet
GET  /api/v1/invoice-review/{review_id}/sheet.csv
GET  /api/v1/invoice-review/{review_id}/preview
GET  /api/v1/invoice-review/{review_id}/apps-script
POST /api/v1/invoice-review/{review_id}/google-sheet
GET  /api/v1/invoice-review/{review_id}/send-page
POST /api/v1/invoice-review/{review_id}/send-from-google-sheet
POST /api/v1/invoice-review/{review_id}/sync-sheet-and-confirm-send
POST /api/v1/invoice-review/{review_id}/confirm-send
GET  /api/v1/invoice-review/exports/iiko
```

### Приемка

```text
POST /api/v1/receiving/start
POST /api/v1/receiving/{receiving_id}/documents
POST /api/v1/receiving/{receiving_id}/compare-invoice
POST /api/v1/receiving/{receiving_id}/corrections
POST /api/v1/receiving/{receiving_id}/corrections/parse
POST /api/v1/receiving/{receiving_id}/corrections/text
POST /api/v1/receiving/{receiving_id}/confirm
GET  /api/v1/receiving/{receiving_id}
GET  /api/v1/receiving/{receiving_id}/accounting-payload
POST /api/v1/receiving/export/google-sheets-mvp
```

### Backoffice, аналитика и iiko MVP

```text
GET  /api/v1/receiving/{receiving_id}/documents
GET  /api/v1/documents/history
GET  /api/v1/documents/{document_id}
GET  /api/v1/documents/{document_id}/view
GET  /api/v1/analytics/discrepancies
GET  /api/v1/suppliers/control
GET  /api/v1/iiko/receivings/{receiving_id}/payload
POST /api/v1/iiko/receivings/{receiving_id}/send
GET  /api/v1/iiko/exports
```

### Учетная система

```text
POST /api/v1/accounting/mappings
GET  /api/v1/accounting/mappings
POST /api/v1/accounting/receivings/{receiving_id}/send
GET  /api/v1/accounting/exports
```

## Настройка `.env`

Создать `.env` можно из примера:

```bash
cp .env.example .env
```

Актуальные основные настройки:

```env
DATABASE_URL=sqlite:///./autosnab_mvp.db

GOOGLE_AUTH_MODE=oauth
GOOGLE_OAUTH_CLIENT_SECRETS_FILE=backend/secrets/oauth-client.json
GOOGLE_OAUTH_TOKEN_FILE=backend/secrets/oauth-token.json
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/api/v1/google-oauth/callback

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
```

## Настройка Google OAuth

1. В Google Cloud Console включить API:

```text
Google Drive API
Google Sheets API
```

2. Создать OAuth Client ID типа:

```text
Web application
```

3. Добавить redirect URI:

```text
http://localhost:8000/api/v1/google-oauth/callback
```

4. Скачать JSON и сохранить его сюда:

```text
backend/secrets/oauth-client.json
```

5. Запустить backend и открыть:

```text
http://localhost:8000/api/v1/google-oauth/authorize
```

6. После успешного входа появится файл:

```text
backend/secrets/oauth-token.json
```

## Локальный запуск

```bash
cd autosnab_mvp
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 --app-dir backend
```

Для Windows:

```bash
cd autosnab_mvp
python -m venv .venv
.venv\Scripts\activate
pip install -r backend\requirements.txt
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 --app-dir backend
```

Открыть страницу загрузки:

```text
http://localhost:8000/api/v1/invoice-review/upload-page
```

Swagger:

```text
http://localhost:8000/docs
```

## Запуск через Docker

```bash
cd autosnab_mvp
docker compose build --no-cache
docker compose up
```

При Docker-запуске нужно убедиться, что папка `backend/secrets` содержит OAuth-файлы, а `.env` указывает корректные пути.

## Пример загрузки накладной через curl

```bash
curl -X POST "http://localhost:8000/api/v1/invoice-review/upload-photo" \
  -F "file=@invoice.jpg" \
  -F "create_google_sheet=true" \
  -F "public_api_base_url=http://localhost:8000"
```

Пример ответа:

```json
{
  "review_id": 1,
  "status": "needs_review",
  "spreadsheet_name": "АвтоСнаб Накладные",
  "csv_path": "exports/invoice_review_1.csv",
  "google_spreadsheet_id": "...",
  "google_spreadsheet_url": "https://docs.google.com/spreadsheets/d/...",
  "next_actions": {
    "open_sheet": "/api/v1/invoice-review/1/sheet",
    "open_csv": "/api/v1/invoice-review/1/sheet.csv",
    "create_google_sheet": "/api/v1/invoice-review/1/google-sheet",
    "send_page": "/api/v1/invoice-review/1/send-page",
    "preview": "/api/v1/invoice-review/1/preview",
    "confirm_send": "/api/v1/invoice-review/1/confirm-send",
    "sync_sheet_and_confirm_send": "/api/v1/invoice-review/1/sync-sheet-and-confirm-send"
  }
}
```

## Проверка и отправка

1. Загрузить накладную через `/api/v1/invoice-review/upload-page`.
2. Открыть созданную Google Таблицу.
3. Проверить и исправить лист `Накладные`.
4. При необходимости заполнить поля УС, статус строки и причину ручной корректировки.
5. Открыть `/api/v1/invoice-review/{review_id}/send-page`.
6. Нажать `Отправить в iiko`.

Backend прочитает данные из Google Таблицы и сформирует актуальный payload для iiko на основе пользовательских исправлений.

## Тесты

```bash
cd autosnab_mvp/backend
pytest
```

Текущий результат проверки:

```text
41 passed
```

## Текущие ограничения

- Google Vision API не используется.
- Основной Google-доступ идет через OAuth пользователя, а не через service account.
- Если OAuth не выполнен, Google Drive OCR и создание Google Таблиц не сработают.
- Внешние AI/API-ключи не используются: разбор выполняется встроенным regex parser.
- Реальная отправка в iiko требует заполненных настроек iiko и включения `IIKO_INTEGRATION_ENABLED=true`.
- При `IIKO_INTEGRATION_ENABLED=false` данные сохраняются как mock/export payload.
- MAX, Telegram, n8n и другие внешние интерфейсы остаются внешним слоем и должны вызывать backend endpoint'ы.
