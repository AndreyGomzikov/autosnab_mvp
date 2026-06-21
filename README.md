# АвтоСнаб — Backend для команды 2

Backend-проект доработан до MVP-4 по сценарию проверки гипотезы автоматической обработки накладных и ручной отправки данных в iiko через промежуточный интерфейс на базе Google Таблицы.

Целевой сценарий:

```text
Фото / файл накладной
↓
распознавание минимального набора данных
↓
вывод данных в Google Таблицу для проверки
↓
ручная проверка пользователем в Google Таблице
↓
предпросмотр того, что уйдет в iiko
↓
ручное подтверждение отправки кнопкой Google Apps Script
↓
статус результата / ошибка отправки
```

## Что реализовано

### Промежуточная проверка накладной перед iiko

Добавлен отдельный бизнес-модуль:

```text
backend/app/routers/invoice_review.py
backend/app/services/invoice_review_service.py
backend/app/schemas/invoice_review.py
```

Endpoint'ы:

```text
POST /api/v1/invoice-review/upload
PUT  /api/v1/invoice-review/{review_id}
GET  /api/v1/invoice-review/{review_id}/sheet
GET  /api/v1/invoice-review/{review_id}/sheet.csv
GET  /api/v1/invoice-review/{review_id}/preview
GET  /api/v1/invoice-review/{review_id}/apps-script
POST /api/v1/invoice-review/{review_id}/confirm-send
GET  /api/v1/invoice-review/exports/iiko
```

### Минимальный набор данных для отправки в iiko

Backend принимает и проверяет следующие данные:

- поставщик;
- юридическое название поставщика, если распознано;
- дата накладной;
- номер накладной;
- заведение / точка доставки;
- адрес доставки, если есть;
- файл / фото накладной;
- raw OCR text;
- товарные позиции;
- наименование товара;
- количество;
- единица измерения;
- цена;
- сумма;
- НДС, если указан;
- комментарии / расхождения;
- confidence распознавания, если есть.

### Google Таблица как единственный промежуточный слой проверки

Endpoint:

```text
GET /api/v1/invoice-review/{review_id}/sheet
```

возвращает структуру, которую можно положить в Google Таблицу:

- лист `Проверка накладной`;
- лист `Товарные позиции`;
- действие для кнопки `Подтвердить и отправить в iiko`.

Endpoint:

```text
GET /api/v1/invoice-review/{review_id}/sheet.csv
```

возвращает CSV-представление этой таблицы.

При загрузке накладной дополнительно создается CSV-файл:

```text
backend/exports/invoice_review_{review_id}.csv
```

### Проверка только через Google Таблицу

В MVP-4 оставлен один промежуточный интерфейс — Google Таблица. Отдельная HTML-страница проверки для MVP-4 не используется. Пользователь проверяет распознанные данные в таблице, видит целевую организацию iiko, поставщика, товары, количества, цены, суммы, статус и ошибки, после чего нажимает кнопку, привязанную к Google Apps Script.

### Предпросмотр отправки в iiko

Endpoint:

```text
GET /api/v1/invoice-review/{review_id}/preview
```

показывает payload, который будет отправлен в iiko:

- target system: `iiko`;
- организация / точка;
- склад;
- поставщик;
- накладная;
- товарные позиции;
- итоговая сумма;
- список замечаний;
- статус до отправки.

### Ручное подтверждение отправки

Endpoint:

```text
POST /api/v1/invoice-review/{review_id}/confirm-send
```

Пример тела запроса:

```json
{
  "approved": true,
  "dry_run": false,
  "target_organization": "Добрая столовая",
  "target_warehouse": "Основной склад",
  "approved_by": "user@example.com",
  "comment": "Проверено вручную"
}
```

Важное правило: если `approved=false`, backend запрещает отправку. Пользователь должен вручную подтвердить проверку накладной.

### Google Apps Script для кнопки

При создании Google Таблицы backend теперь автоматически устанавливает container-bound Google Apps Script через Google Apps Script API, если включено:

```env
GOOGLE_APPS_SCRIPT_ENABLED=true
```

После открытия таблицы у пользователя появляется меню:

```text
АвтоСнаб → Предпросмотр отправки
АвтоСнаб → Отправить в iiko
```

Endpoint ниже оставлен как резервный способ получить исходный код скрипта вручную:

```text
GET /api/v1/invoice-review/{review_id}/apps-script
```

Если Google Apps Script API недоступен или не включен в Google Cloud, проект все равно создает таблицу и сохраняет код на резервном листе `Apps Script backup`.

## Ранее реализованные возможности:

### Приемка

- `POST /api/v1/receiving/start` — старт приемки.
- `POST /api/v1/receiving/{receiving_id}/documents` — сохранение документа/накладной.
- `POST /api/v1/receiving/{receiving_id}/compare-invoice` — сверка накладной с заявкой.
- `POST /api/v1/receiving/{receiving_id}/corrections` — применение JSON-корректировок.
- `POST /api/v1/receiving/{receiving_id}/corrections/parse` — разбор текстовой корректировки в JSON-команду.
- `POST /api/v1/receiving/{receiving_id}/corrections/text` — разбор и применение текстовой корректировки.
- `POST /api/v1/receiving/{receiving_id}/confirm` — подтверждение полной/частичной приемки.
- `GET /api/v1/receiving/{receiving_id}/accounting-payload` — payload для учетной системы.
- `POST /api/v1/receiving/export/google-sheets-mvp` — CSV-выгрузка листов приемки.

### Учетная система / iiko MVP

- `GET /api/v1/iiko/receivings/{receiving_id}/payload` — iiko-oriented payload для приходного документа.
- `POST /api/v1/iiko/receivings/{receiving_id}/send` — mock-отправка в iiko.
- `GET /api/v1/iiko/exports` — история iiko-экспортов.
- `POST /api/v1/accounting/mappings` — ручная связь товара поставщика с номенклатурой учетной системы.
- `GET /api/v1/accounting/mappings` — список связей.
- `POST /api/v1/accounting/receivings/{receiving_id}/send` — mock-передача приемки в учетную систему.
- `GET /api/v1/accounting/exports` — история подготовленных/отправленных payload.

### Накладные, аналитика и контроль поставщиков

- `GET /api/v1/receiving/{receiving_id}/documents` — накладные конкретной приемки.
- `GET /api/v1/documents/history` — история всех накладных с фильтрами `supplier` и `venue`.
- `GET /api/v1/documents/{document_id}` — данные одной накладной.
- `GET /api/v1/documents/{document_id}/view` — HTML-страница просмотра накладной.
- `GET /api/v1/analytics/discrepancies` — аналитика расхождений по всем приемкам.
- `GET /api/v1/suppliers/control` — контроль поставщиков с risk score и статусами `ok`, `watch`, `control_required`.

## Запуск локально

```bash
cd autosnab_mvp
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
cd backend
uvicorn app.main:app --reload
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

## Тесты

```bash
cd backend
pytest
```

Текущий результат проверки:

```text
19 passed
```

## Ограничения

- Реальный OCR/Vision подключен через Google Cloud Vision, но работает только при наличии Google credentials и `GOOGLE_VISION_ENABLED=true`.
- Реальное создание Google Таблицы подключено через Google Sheets API, но работает только при наличии Google credentials и `GOOGLE_SHEETS_ENABLED=true`.
- Промежуточный интерфейс реализован только через Google Таблицу. HTML-интерфейс проверки не используется.
- iiko-интеграция выполнена как MVP adapter/mock: формируется прозрачный payload и сохраняется export. Для реальной отправки нужны URL, токен, организация, склад, поставщик и правила конкретного iiko API.
- MAX/Telegram/n8n остаются внешним слоем, который должен вызывать backend endpoint'ы.

## Реальный OCR и реальное создание Google Таблицы через API

В текущей версии добавлен полный интеграционный контур для реального сценария:

```text
Фото / файл накладной
↓
POST /api/v1/invoice-review/upload-photo
↓
Google Cloud Vision OCR распознает текст
↓
backend формирует первичный JSON накладной
↓
backend создает реальную Google Таблицу через Google Sheets API
↓
пользователь проверяет и исправляет данные в Google Таблице
↓
Google Apps Script читает исправленные строки из таблицы
↓
POST /api/v1/invoice-review/{review_id}/sync-sheet-and-confirm-send
↓
backend формирует payload и отправляет данные в iiko adapter
```

### Настройки Google API

Для реального OCR и создания Google Таблицы нужно создать service account в Google Cloud, выдать ему доступ к Google Vision API, Google Sheets API и Google Drive API, скачать JSON credentials и указать путь к файлу.

Пример `.env` для текущей настройки:

```env
GOOGLE_VISION_ENABLED=true
GOOGLE_SHEETS_ENABLED=true
GOOGLE_SERVICE_ACCOUNT_FILE=backend/secrets/service-account.json
GOOGLE_DRIVE_FOLDER_ID=1itNgeCfFjyDUB1--cltD-wAh_SfMCzOG
PUBLIC_API_BASE_URL=http://localhost:8000
UPLOADED_INVOICES_DIR=uploads/invoices
```

Файл `service-account.json` нужно положить вручную в папку:

```text
backend/secrets/service-account.json
```

Настоящий JSON-ключ не включается в архив проекта и не должен попадать в GitHub.

Также можно использовать стандартную переменную:

```env
GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/service-account.json
```


### Текущая Google Drive папка

В проект уже добавлен ID папки Google Drive для создания таблиц:

```env
GOOGLE_DRIVE_FOLDER_ID=1itNgeCfFjyDUB1--cltD-wAh_SfMCzOG
```

Сервисный аккаунт должен иметь доступ к этой папке с ролью `Editor / Редактор`.

### Загрузка реального фото накладной

Пример запроса:

```bash
curl -X POST "http://localhost:8000/api/v1/invoice-review/upload-photo" \
  -F "file=@invoice.jpg" \
  -F "venue=Добрая столовая" \
  -F "delivery_address=ул. Тверская" \
  -F "create_google_sheet=true" \
  -F "public_api_base_url=http://localhost:8000"
```

Ответ содержит:

```json
{
  "review_id": 1,
  "status": "needs_review",
  "google_spreadsheet_id": "...",
  "google_spreadsheet_url": "https://docs.google.com/spreadsheets/d/...",
  "next_actions": {
    "apps_script": "/api/v1/invoice-review/1/apps-script",
    "sync_sheet_and_confirm_send": "/api/v1/invoice-review/1/sync-sheet-and-confirm-send"
  }
}
```

### Как работает кнопка в Google Таблице

При создании Google Таблицы backend вызывает Google Apps Script API и автоматически привязывает к таблице скрипт `Code.gs`. Пользователю не нужно копировать код вручную.

Скрипт читает фактически отредактированные пользователем данные из листов `Проверка накладной` и `Товарные позиции`, затем отправляет их в backend через:

```text
POST /api/v1/invoice-review/{review_id}/sync-sheet-and-confirm-send
```

Так пользовательские исправления из Google Таблицы действительно попадают в отправляемый iiko payload. На листе `Отправка в iiko` показываются статус установки Apps Script, ссылка на script project, последний статус отправки и время отправки.

### Что остается mock

Распознавание OCR и создание Google Таблицы теперь реализованы как реальные внешние интеграции через Google API при наличии credentials. Отправка в iiko по-прежнему выполнена как adapter/mock, потому что для production-отправки нужны реальные параметры iiko клиента: URL/API, авторизация, организация, склад, поставщик и ID номенклатуры.


## AI Agent для структурирования OCR-текста

Добавлен AI Agent как отдельный слой между OCR и Google Таблицей:

```text
Фото / файл накладной
↓
Google Cloud Vision OCR получает сырой текст
↓
AI Agent превращает сырой OCR-текст в структурированный JSON накладной
↓
backend создает Google Таблицу
↓
пользователь проверяет данные и вручную отправляет в iiko
```

Новый файл:

```text
backend/app/services/ai_invoice_agent_service.py
```

AI Agent не отправляет данные в iiko и не меняет базу напрямую. Он только извлекает структуру:

- поставщик;
- юридическое название поставщика;
- номер накладной;
- дату накладной;
- точку доставки;
- товарные позиции;
- количество;
- единицу измерения;
- цену;
- сумму;
- НДС;
- комментарии и confidence.

Если AI Agent отключен или вернул ошибку, backend автоматически использует старый deterministic fallback parser, чтобы локальная разработка и тесты не зависели от внешнего AI API.

### Настройки AI Agent

Пример `.env`:

```env
AI_AGENT_ENABLED=true
AI_AGENT_BASE_URL=https://api.openai.com/v1/chat/completions
AI_AGENT_API_KEY=your_api_key
AI_AGENT_MODEL=gpt-4o-mini
AI_AGENT_TEMPERATURE=0.1
AI_AGENT_TIMEOUT_SECONDS=60
AI_AGENT_MAX_OCR_CHARS=12000
```

При `AI_AGENT_ENABLED=false` endpoint `POST /api/v1/invoice-review/upload-photo` продолжит работать через OCR + fallback parser.

## Текущий результат тестов

```text
19 passed
```

## Доработка под документацию iiko Server API: приходная накладная XML

Добавлена подготовка реального XML для метода iiko Server API:

```text
POST /resto/api/documents/import/incomingInvoice
Content-Type: application/xml
```

Теперь перед отправкой в iiko формируется XML `incomingInvoiceDto` с шапкой документа и строками `incomingInvoiceItemDto`.

### Поля в Google Таблице

В Google Таблице пользователь видит и редактирует только бизнес-поля накладной:

```text
Поставщик
Номер накладной
Дата накладной
Заведение / точка доставки
Склад / подразделение как обычное название
Наименование товара
Количество
Ед. изм.
Цена
Сумма
НДС %
НДС сумма
Комментарий пользователя
```

Технические iiko-поля не хранятся в Google Таблице. Backend держит их в базе и заполняет автоматически через справочники iiko:

```text
iiko supplier id
iiko defaultStore / склад
product или productArticle
amountUnit
vatPercent / vatSum
mapping_status / mapping_error
```

Пользователь проверяет понятные данные и нажимает меню:

```text
АвтоСнаб → Предпросмотр отправки
АвтоСнаб → Отправить в iiko
```

### Новые проверки перед отправкой

Перед отправкой backend проверяет:

```text
есть поставщик;
есть номер и дата накладной;
есть склад iiko/defaultStore;
есть товарные позиции;
по каждой позиции есть product или productArticle;
по каждой позиции есть num;
по каждой позиции есть amount, amountUnit, price, sum.
```

Если данные неполные, отправка блокируется, кроме случая `allow_with_warnings=true`.

### Реальная iiko-интеграция

По умолчанию отправка остается безопасной: XML готовится, но реальный запрос в iiko не выполняется.

Для реальной отправки нужно заполнить `.env`:

```env
IIKO_INTEGRATION_ENABLED=true
IIKO_BASE_URL=https://host:port
IIKO_LOGIN=api_user
IIKO_PASSWORD_SHA1=sha1_hash_password
# либо готовый токен вместо логина/пароля
IIKO_TOKEN=
```

Если `IIKO_INTEGRATION_ENABLED=false`, backend сохраняет экспорт со статусом `iiko_sent_mock`, но в payload уже есть ключ `iikoXml`, который можно проверить вручную или использовать для дальнейшей интеграции.

## Автоматическое заполнение технических полей iiko

Для реальной отправки приходной накладной система теперь пытается сама заполнить технические поля iiko из справочников клиента:

- `iiko_supplier_id` — из `/resto/api/suppliers`;
- `iiko_default_store_id` / `defaultStore` — из `/resto/api/v2/entities/list?rootType=Account`;
- `iiko_product_id` или `productArticle` — из `/resto/api/v2/entities/products/list`;
- `amountUnit` — из `/resto/api/v2/entities/list?rootType=MeasureUnit`;
- `vatPercent` / `vatSum` — по налоговым категориям, если ставка доступна.

Пользователь в Google Таблице не обязан вручную вводить UUID iiko. Для MVP-4 пользователь редактирует только бизнес-поля:

- `Поставщик`;
- `Номер накладной`;
- `Дата накладной`;
- `Заведение / точка доставки`;
- `Склад / подразделение`, только если система не определила его автоматически;
- `Наименование товара`;
- `Количество`;
- `Ед. изм.`;
- `Цена`;
- `Сумма`;
- `НДС %`;
- `НДС сумма`;
- `Комментарий пользователя`.

Технические поля iiko **не записываются в Google Таблицу**. Они хранятся в backend в metadata накладной и используются при предпросмотре/отправке. Если пользователь меняет бизнес-поля в Google Таблице, backend повторно запускает автосопоставление по справочникам iiko.

Если система уверенно нашла поставщика, склад, товар и единицу измерения, строка получает статус `ready`. Если уверенность низкая или справочник недоступен, строка получает статус `needs_review`, а отправка блокируется до исправления или ручного разрешения `allow_with_warnings=true`.

Новые endpoint'ы:

```text
GET  /api/v1/invoice-review/iiko/references/status
POST /api/v1/invoice-review/{review_id}/iiko-auto-map?force_refresh=true
```

Настройки:

```env
IIKO_INTEGRATION_ENABLED=true
IIKO_BASE_URL=https://your-iiko-host:port
IIKO_LOGIN=api_user
IIKO_PASSWORD_SHA1=sha1_hash
IIKO_AUTO_MAPPING_ENABLED=true
IIKO_MAPPING_MIN_CONFIDENCE=0.72
```

Если `IIKO_INTEGRATION_ENABLED=false`, система не обращается в настоящий iiko, оставляет уже заполненные поля и помечает отсутствующие поля как `needs_review`.


## Актуальная логика Google Таблицы

Правильная схема теперь такая:

```text
Лист "Проверка накладной"      → пользователь проверяет шапку накладной
Лист "Товарные позиции"        → пользователь проверяет товары, количество, цену, сумму, НДС
Лист "Отправка в iiko"         → статус Apps Script, статус отправки и сообщение об ошибке
Лист "Apps Script backup"      → резервная копия кода; скрывается, если автоматическая установка прошла успешно
```

Пользователь вручную заполняет только бизнес-поля накладной. Служебные iiko-поля заполняются backend-ом и остаются в базе проекта, а не в Google Таблице. Они берутся через:

```text
/resto/api/suppliers
/resto/api/v2/entities/products/list
/resto/api/v2/entities/list?rootType=Account
/resto/api/v2/entities/list?rootType=MeasureUnit
/resto/api/v2/entities/list?rootType=TaxCategory
```

Если система не смогла сопоставить товар, поставщика, склад или единицу измерения, пользователь видит в строке статус `Нужно проверить` и исправляет обычные поля: название товара, количество, единицу, цену, сумму или НДС.
После нажатия `АвтоСнаб → Отправить в iiko` backend повторно синхронизирует исправления и снова пытается заполнить служебные iiko-поля.
