# Интеграция с Диадок

## Автоматический сценарий

После запуска FastAPI встроенный scheduler опрашивает входящую ленту Диадок через `V8/GetNewEvents`. Интервал задаётся `DIADOC_SYNC_INTERVAL_SECONDS`, по умолчанию — 300 секунд.

Цепочка обработки:

1. Получение входящих событий и сохранение `afterIndexKey`.
2. Получение метаданных сообщения через `V6/GetMessage`.
3. Скачивание содержимого сущностей через `V4/GetEntityContent`.
4. Сохранение XML, PDF, изображений и остальных вложений в `uploads/diadoc/<message_id>/`.
5. Прямой разбор формализованного XML либо запуск общего extraction pipeline для PDF/изображений.
6. Поиск существующей заявки по номеру документа-основания. Если заявка найдена, запускается сравнение; иначе создаётся карточка ручной проверки.
7. Постановка независимых задач доставки печатной PDF-формы и данных в Google Sheets.
8. Повтор временно неуспешных операций с экспоненциальной задержкой. После исчерпания попыток задача получает статус `dead_letter`.

Формализованный XML обрабатывается первым. Ранее скачанные и новые вложения того же сообщения прикрепляются к одной карточке проверки.

## Надёжность

Бизнес-обработка документа и внешние доставки разделены:

- документ может быть успешно разобран даже при временной недоступности Google Sheets или печатной формы;
- отдельная таблица `diadoc_deliveries` хранит состояние доставки в Google Sheets и получения PDF;
- повторная запись в Google Sheets защищена локальной записью экспорта и проверкой `ID документа` в целевом листе;
- `diadoc_leases` не позволяет нескольким Uvicorn workers одновременно выполнять одну синхронизацию;
- загруженные ранее вложения прикрепляются к карточке, когда основной XML появляется позже.

Таблицы создаются при обычном старте приложения. Для явного запуска создания таблиц:

```bash
cd backend
python scripts/migrate_diadoc_reliability.py
```

## OIDC

Используется Authorization Code Flow OpenID Connect:

- начало авторизации: `GET /api/v1/diadoc/oauth/authorize`;
- callback: `GET /api/v1/diadoc/oauth/callback`;
- состояние токена: `GET /api/v1/diadoc/oauth/status`;
- удаление локальных токенов: `POST /api/v1/diadoc/oauth/logout`.

После успешного callback scheduler запускается сразу; перезапуск backend после первой авторизации не требуется. Перед API-вызовом access token обновляется заранее, а после ответа `401` выполняется один принудительный refresh и повтор запроса.

## Защита административных endpoints

Для запросов статуса, ручной синхронизации, retry и управления OAuth используется заголовок:

```text
X-Diadoc-Api-Key: <DIADOC_ADMIN_API_KEY>
```

Если `DIADOC_ADMIN_API_KEY` пуст, используется `BOT_API_SHARED_SECRET`. Если оба значения пусты, защита отключена только для удобства локальной разработки. OAuth callback остаётся публичным, поскольку на него перенаправляет Identity Kontur.

## Диагностические endpoints

- `GET /api/v1/diadoc/preflight` — проверка OAuth, BoxId, доступа к API, каталога файлов и базовой готовности;
- `GET /api/v1/diadoc/status` — конфигурация, последний запуск и размеры очередей;
- `GET /api/v1/diadoc/scheduler/status` — состояние scheduler;
- `GET /api/v1/diadoc/organizations` — доступные организации и ящики;
- `POST /api/v1/diadoc/sync` — принудительная синхронизация;
- `POST /api/v1/diadoc/retry` — повтор документов и доставок;
- `GET /api/v1/diadoc/dead-letter` — необработанные документы и доставки;
- `POST /api/v1/diadoc/dead-letter/retry-all` — повтор всех dead-letter задач;
- `POST /api/v1/diadoc/dead-letter/{document_id}/retry` — повтор одного документа.

## Переменные окружения

```env
DIADOC_INTEGRATION_ENABLED=true
DIADOC_API_BASE_URL=https://diadoc-api.kontur.ru
DIADOC_OAUTH_AUTHORIZE_URI=https://identity.kontur.ru/connect/authorize
DIADOC_OAUTH_TOKEN_URI=https://identity.kontur.ru/connect/token
DIADOC_OAUTH_REDIRECT_URI=https://<backend>/api/v1/diadoc/oauth/callback
DIADOC_OAUTH_SCOPE=openid profile email offline_access Diadoc.PublicAPI
DIADOC_CLIENT_ID=
DIADOC_CLIENT_SECRET=
DIADOC_ACCESS_TOKEN=
DIADOC_REFRESH_TOKEN=
DIADOC_TOKEN_EXPIRY=
DIADOC_BOX_ID=
DIADOC_DEPARTMENT_ID=
DIADOC_TYPE_NAMED_IDS=
DIADOC_TIMEOUT_SECONDS=30
DIADOC_SYNC_LIMIT=100
DIADOC_DOCUMENTS_DIR=uploads/diadoc
DIADOC_SCHEDULER_ENABLED=true
DIADOC_SYNC_INTERVAL_SECONDS=300
DIADOC_INITIAL_SYNC_MODE=latest
DIADOC_MAX_PAGES_PER_SYNC=10
DIADOC_HTTP_RETRY_ATTEMPTS=4
DIADOC_HTTP_RETRY_BASE_DELAY_SECONDS=1
DIADOC_HTTP_RETRY_MAX_DELAY_SECONDS=30
DIADOC_MAX_ATTACHMENT_BYTES=100000000
DIADOC_MAX_XML_BYTES=20000000
DIADOC_RETRY_MAX_ATTEMPTS=5
DIADOC_RETRY_BASE_DELAY_SECONDS=60
DIADOC_RETRY_BATCH_SIZE=50
DIADOC_DELIVERY_STALE_SECONDS=600
DIADOC_SYNC_LEASE_SECONDS=1800
DIADOC_GENERATE_PRINT_FORM=true
DIADOC_PRINT_FORM_ATTEMPTS=5
DIADOC_DOWNLOAD_ALL_ATTACHMENTS=true
DIADOC_PARSE_UNSTRUCTURED_ATTACHMENTS=true
DIADOC_UNSTRUCTURED_EXTRACTION_METHOD=openai
DIADOC_ADMIN_API_KEY=
```


## Надёжность чтения реального ящика

- При первом запуске используется `DIADOC_INITIAL_SYNC_MODE=latest`: сервис фиксирует текущий последний `IndexKey` и обрабатывает только документы, поступившие после подключения. Для намеренной загрузки истории задайте `oldest`.
- За один цикл вычитывается до `DIADOC_MAX_PAGES_PER_SYNC` страниц ленты, а курсор сохраняется после каждой страницы.
- Временные сетевые ошибки и HTTP 408/425/429/5xx повторяются с экспоненциальной задержкой и учётом `Retry-After`.
- Вложения `GetEntityContent` читаются потоково и прерываются при превышении `DIADOC_MAX_ATTACHMENT_BYTES`, чтобы чрезмерный файл не занял всю память процесса.
- Служебные XML-подтверждения, подписи и извещения сохраняются как артефакты, но не передаются в парсер накладных.
- XML с `DTD/ENTITY`, зашифрованные и чрезмерно большие сущности переводятся непосредственно в `dead_letter` как постоянные ошибки.

## Первый запуск

1. Получите `client_id`, `client_secret` и зарегистрируйте callback URL.
2. Заполните `DIADOC_CLIENT_ID`, `DIADOC_CLIENT_SECRET`, `DIADOC_BOX_ID` и `DIADOC_OAUTH_REDIRECT_URI`.
3. Запустите backend.
4. Откройте `/api/v1/diadoc/oauth/authorize`, передав административный ключ, и завершите вход.
5. Проверьте `/api/v1/diadoc/status` и `/api/v1/diadoc/scheduler/status`.
6. Отправьте тестовый XML/PDF в ящик и проверьте карточку, вложения и Google Sheets.

Внешний n8n Schedule Trigger не обязателен. Его можно использовать только как дополнительный watchdog.
