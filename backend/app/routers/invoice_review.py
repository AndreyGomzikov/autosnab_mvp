import html
import json
import mimetypes
import shutil
from threading import Thread
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db.session import SessionLocal, get_db
from app.models.accounting import AccountingExport
from app.models.receiving import Receiving, ReceivingDocument
from app.schemas.invoice_review import (
    BotDraftInfo,
    BotDraftPageResponse,
    BotDraftResetResponse,
    BotDraftStatusResponse,
    BotUploadAcceptedResponse,
    BotDocumentSummary,
    BotUploadStatusResponse,
    ConfirmSendToIikoRequest,
    InvoiceReviewCreateRequest,
    InvoiceReviewResponse,
    InvoiceReviewUpdateRequest,
    PipelineLogEntry,
    RecognizedInvoiceItem,
    SyncSheetAndConfirmRequest,
)
from app.services.bot_ingestion_service import (
    DRAFT_STATUS,
    append_draft_file,
    build_bot_document_summary,
    build_bot_next_actions,
    build_source_metadata,
    classify_bot_file,
    create_upload_journal,
    delete_draft,
    derive_bot_result,
    draft_display_name,
    get_active_draft,
    get_latest_upload_for_chat,
    get_upload_journal,
    list_draft_files,
    update_upload_journal,
)
from app.services.invoice_review_service import (
    build_apps_script_sample,
    build_iiko_preview,
    build_review_csv,
    build_review_sheet,
    create_real_google_sheet_for_review,
    confirm_and_send_to_iiko,
    create_invoice_review,
    save_review_csv,
    sync_sheet_and_confirm_to_iiko,
    update_invoice_review,
    remap_review_with_iiko_references,
    get_iiko_reference_status,
    get_latest_google_spreadsheet_info,
    send_google_sheet_and_confirm_to_iiko,
)
from app.services.database_health_service import describe_database_write_error
from app.services.document_extraction_service import extract_invoice_document, extract_invoice_document_set
from app.services.google_sheets_service import load_invoice_reference_catalogs
from app.services.item_normalization_service import apply_reference_mapping_to_payload
from app.services.normalization import canonical_invoice_number
from app.services.upload_trace_service import (
    append_trace_log,
    finalize_trace,
    get_trace,
    initialize_trace,
    set_trace_metadata,
    set_trace_result,
)

router = APIRouter(prefix="/invoice-review", tags=["invoice-review"])


def require_bot_api_key(x_bot_api_key: str | None = Header(default=None)) -> None:
    if settings.bot_api_shared_secret and x_bot_api_key != settings.bot_api_shared_secret:
        raise HTTPException(status_code=401, detail="Неверный или отсутствующий X-Bot-Api-Key.")




@router.get("/upload-page", response_class=HTMLResponse)
def upload_invoice_page():
    return HTMLResponse("""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>АвтоСнаб — загрузка накладной</title>
  <style>
    :root { color-scheme: light; }
    body {
      margin: 0;
      font-family: Arial, sans-serif;
      background: #f5f7fb;
      color: #1f2937;
    }
    .page {
      max-width: 920px;
      margin: 0 auto;
      padding: 32px 16px;
    }
    .card {
      background: white;
      border: 1px solid #e5e7eb;
      border-radius: 16px;
      box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
      padding: 28px;
    }
    h1 { margin: 0 0 8px; font-size: 28px; text-align: center; }
    .subtitle { color: #6b7280; margin-bottom: 24px; line-height: 1.5; }
    label { display: block; font-weight: 700; margin-bottom: 8px; }
    .checkbox-label {
      display: flex; align-items: center; gap: 10px; margin: 16px 0 10px;
      font-weight: 700; cursor: pointer;
    }
    .checkbox-label input { width: 18px; height: 18px; }
    .hint { color: #6b7280; font-size: 14px; line-height: 1.45; margin-top: 8px; }
    .selected-files {
      margin-top: 12px;
      border: 1px solid #dbeafe;
      background: #eff6ff;
      border-radius: 12px;
      padding: 12px 14px;
    }
    .selected-files-title { font-weight: 700; margin-bottom: 8px; }
    .selected-files ol { margin: 0; padding-left: 0; list-style: none; }
    .selected-files li {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin: 6px 0;
      line-height: 1.35;
    }
    .selected-file-name { min-width: 0; overflow-wrap: anywhere; }
    .selected-file-actions {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      justify-content: flex-end;
      flex-shrink: 0;
    }
    .file-size { color: #6b7280; font-size: 13px; }
    .remove-file-btn,
    .move-file-btn {
      min-width: auto;
      padding: 4px 8px;
      border-radius: 8px;
      background: #e5e7eb;
      color: #374151;
      font-size: 12px;
      vertical-align: middle;
    }
    .duplicate-hint { color: #b45309; font-size: 14px; line-height: 1.45; margin-top: 8px; }
    input[type="file"] {
      width: 100%; box-sizing: border-box; padding: 12px;
      border: 1px solid #d1d5db; border-radius: 10px; background: #fff;
      font-size: 15px;
      color: transparent;
    }
    input[type="file"]::file-selector-button {
      color: #1f2937;
      margin-right: 0;
    }
    input[type="file"]::-webkit-file-upload-button {
      color: #1f2937;
      margin-right: 0;
    }
    select {
      width: 100%; box-sizing: border-box; padding: 12px;
      border: 1px solid #d1d5db; border-radius: 10px; background: #fff;
      font-size: 15px;
      margin-top: 4px;
    }
    .field {
      margin-bottom: 18px;
    }
    .panel {
      margin-bottom: 22px;
      padding: 18px;
      border: 1px solid #dbeafe;
      border-radius: 14px;
      background: linear-gradient(135deg, #f8fbff 0%, #eef4ff 100%);
    }
    .panel-title {
      font-size: 16px;
      font-weight: 700;
      margin: 0 0 8px;
    }
    .hint {
      color: #6b7280;
      font-size: 14px;
      line-height: 1.5;
      margin-top: 6px;
    }
    button {
      border: 0; border-radius: 12px; padding: 14px 18px;
      background: #2563eb; color: white; font-weight: 700; font-size: 16px;
      cursor: pointer;
      min-width: 240px;
    }
    button:disabled { opacity: .6; cursor: not-allowed; }
    .secondary-btn {
      display: inline-block;
      text-decoration: none;
      text-align: center;
      border: 0; border-radius: 12px; padding: 14px 18px;
      background: #2563eb; color: white; font-weight: 700; font-size: 16px;
      min-width: 240px;
      box-sizing: border-box;
      margin-top: 10px;
    }
    .status-box {
      margin-top: 22px;
      padding: 18px;
      border-radius: 12px;
      line-height: 1.6;
      border: 1px solid #d1fae5;
      background: #f0fdf4;
    }
    .status-box.warning {
      border-color: #fde68a;
      background: #fffbeb;
    }
    .status-line {
      font-size: 18px;
      font-weight: 700;
      margin-bottom: 6px;
      text-align: center;
    }
    .button-row {
      margin-top: 20px;
      text-align: center;
    }
    .button-row.compact {
      margin-top: 14px;
      text-align: left;
    }
    .button-row.compact button,
    .button-row.compact .secondary-btn {
      min-width: 0;
      margin-right: 10px;
      margin-top: 0;
    }
    .result-actions {
      text-align: center;
    }
    .loading {
      margin-top: 22px;
      padding: 16px;
      border-radius: 12px;
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      line-height: 1.45;
    }
    .error {
      margin-top: 22px;
      padding: 16px;
      border-radius: 12px;
      background: #fef2f2;
      border: 1px solid #fecaca;
      line-height: 1.45;
      white-space: pre-wrap;
    }
    .pipeline-log-box {
      margin-top: 18px;
      padding: 16px;
      border-radius: 12px;
      background: #0f172a;
      color: #e2e8f0;
      border: 1px solid #1e293b;
    }
    .pipeline-log-title {
      font-size: 16px;
      font-weight: 700;
      margin-bottom: 12px;
    }
    .pipeline-log-entry {
      padding: 12px 0;
      border-top: 1px solid rgba(148, 163, 184, 0.2);
    }
    .pipeline-log-entry:first-child {
      border-top: 0;
      padding-top: 0;
    }
    .pipeline-log-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-size: 14px;
      margin-bottom: 6px;
    }
    .pipeline-log-stage {
      font-weight: 700;
      color: #93c5fd;
    }
    .pipeline-log-status {
      font-weight: 700;
      text-transform: uppercase;
    }
    .pipeline-log-status.ok {
      color: #86efac;
    }
    .pipeline-log-status.warning {
      color: #fde68a;
    }
    .pipeline-log-status.error {
      color: #fca5a5;
    }
    .pipeline-log-status.running {
      color: #c4b5fd;
    }
    .pipeline-log-message {
      font-size: 14px;
      line-height: 1.5;
      margin-bottom: 6px;
    }
    .pipeline-log-details {
      font-size: 13px;
      line-height: 1.5;
      color: #cbd5e1;
      white-space: pre-wrap;
    }
    .pipeline-log-recommendation {
      margin-top: 6px;
      color: #fcd34d;
      font-size: 13px;
    }
  </style>
</head>
<body>
  <main class="page">
    <section class="card">
      <h1>АвтоСнаб - Загрузка накладной</h1>
      <div class="subtitle">
        Загрузите фото, скан или PDF накладной. OpenAI vision parser структурирует evidence из PDF, MinerU, OCR и самих изображений страниц.
      </div>
      <div class="panel">
        <div class="panel-title">Google Sheets доступ</div>
        <div id="googleAuthStatus" class="hint">Проверяю статус Google OAuth...</div>
        <div class="button-row compact">
          <button id="googleAuthBtn" type="button">Авторизоваться в Google</button>
          <button id="googleAuthRefreshBtn" type="button">Обновить статус</button>
        </div>
      </div>
      <form id="uploadForm">
        <div class="field">
          <label class="checkbox-label" for="multipageInvoice">
            <input id="multipageInvoice" name="multipage_invoice" type="checkbox" />
            <span>Многостраничная накладная</span>
          </label>
          <label for="file">Файл накладной</label>
          <input id="file" name="files" type="file" accept="image/*,.pdf" capture="environment" />
          <div class="hint" id="fileHint">Если накладная одностраничная, выберите один файл.</div>
          <div class="duplicate-hint" id="duplicateHint"></div>
          <div class="selected-files" id="selectedFilesBox" hidden>
            <div class="selected-files-title">Выбранные файлы:</div>
            <ol id="selectedFilesList"></ol>
          </div>
        </div>
        <div class="field">
          <label for="extractionMethod">Метод распознавания</label>
          <select id="extractionMethod" name="extraction_method">
            <option value="openai" selected>OpenAI vision parser (text + images)</option>
            <option value="google_ocr">Google OCR</option>
            <option value="mineru">MinerU</option>
            <option value="hybrid">Гибрид: MinerU -> Google OCR fallback</option>
          </select>
          <div class="hint">
            OpenAI получает извлеченный текст и изображения страниц, но не управляет Google Sheets и не выбирает колонки таблицы.
          </div>
        </div>
        <div class="button-row">
          <button id="submitBtn" type="submit">Загрузить накладную заведения</button>
        </div>
      </form>
      <div id="output"></div>
    </section>
  </main>

  <script>
    const form = document.getElementById('uploadForm');
    const output = document.getElementById('output');
    const button = document.getElementById('submitBtn');
    const fileInput = document.getElementById('file');
    const multipageCheckbox = document.getElementById('multipageInvoice');
    const fileHint = document.getElementById('fileHint');
    const duplicateHint = document.getElementById('duplicateHint');
    const selectedFilesBox = document.getElementById('selectedFilesBox');
    const selectedFilesList = document.getElementById('selectedFilesList');
    const extractionMethodInput = document.getElementById('extractionMethod');
    const googleAuthStatus = document.getElementById('googleAuthStatus');
    const googleAuthBtn = document.getElementById('googleAuthBtn');
    const googleAuthRefreshBtn = document.getElementById('googleAuthRefreshBtn');

    const sheetWindowName = 'autosnab_google_sheet';
    const submitButtonDefaultText = button.textContent;
    let sheetWindow = null;
    let activeTraceId = null;
    let tracePollTimer = null;
    let selectedFiles = [];
    let uploadInProgress = false;

    function setUploadButtonBusy(isBusy) {
      uploadInProgress = Boolean(isBusy);
      button.disabled = uploadInProgress;
      button.textContent = submitButtonDefaultText;
    }

    const methodLabels = {
      openai: 'OpenAI vision parser (text + images)',
      google_ocr: 'Google OCR',
      mineru: 'MinerU',
      hybrid: 'гибридный режим'
    };

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>'"]/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
      }[ch]));
    }

    function getUserTimezone() {
      try {
        return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
      } catch (error) {
        return '';
      }
    }

    function getUserUtcOffsetMinutes() {
      try {
        return String(-new Date().getTimezoneOffset());
      } catch (error) {
        return '';
      }
    }

    function openSheetInSingleTab(url) {
      const targetUrl = String(url || '').trim();
      if (!targetUrl) {
        return false;
      }
      sheetWindow = window.open(targetUrl, sheetWindowName);
      if (sheetWindow) {
        sheetWindow.focus();
      }
      return false;
    }

    output.addEventListener('click', (event) => {
      const link = event.target.closest('[data-google-sheet-url]');
      if (!link) {
        return;
      }
      event.preventDefault();
      openSheetInSingleTab(link.getAttribute('data-google-sheet-url'));
    });

    function renderPipelineLogs(logs) {
      if (!Array.isArray(logs) || !logs.length) {
        return '';
      }
      const entries = logs.map(log => {
        const details = log && log.details ? Object.entries(log.details)
          .filter(([, value]) => value !== null && value !== undefined && value !== '')
          .map(([key, value]) => `${key}: ${typeof value === 'object' ? JSON.stringify(value, null, 2) : value}`)
          .join('\\n') : '';
        return `
          <div class="pipeline-log-entry">
            <div class="pipeline-log-head">
              <span class="pipeline-log-stage">${escapeHtml(log.stage || 'unknown')}</span>
              <span class="pipeline-log-status ${escapeHtml(log.status || 'warning')}">${escapeHtml(log.status || 'warning')}</span>
            </div>
            <div class="pipeline-log-message">${escapeHtml(log.message || '')}</div>
            ${details ? `<div class="pipeline-log-details">${escapeHtml(details)}</div>` : ''}
            ${log.recommendation ? `<div class="pipeline-log-recommendation">Рекомендация: ${escapeHtml(log.recommendation)}</div>` : ''}
          </div>
        `;
      }).join('');
      return `<div class="pipeline-log-box"><div class="pipeline-log-title">Логи обработки документа</div>${entries}</div>`;
    }

    function renderTraceResult(result) {
      if (!result) {
        return '';
      }
      const hasGoogleSheet = Boolean(result.google_spreadsheet_url);
      const methodInfo = '';
      const traceMeta = '';
      const ocrError = result.ocr && result.ocr.error ? `<div>⚠️ OCR не сработал: ${escapeHtml(result.ocr.error)}</div>` : '';
      const sheetError = result.google_spreadsheet_error ? `<div>Ошибка Google Таблицы: ${escapeHtml(result.google_spreadsheet_error)}</div>` : '';
      if (hasGoogleSheet) {
        return `
          <div class="status-box">
            <div class="status-line">✅ Накладная обработана успешно.</div>
            <div class="status-line">✅ Данные добавлены в таблицу заведения.</div>
            ${methodInfo}
            ${traceMeta}
            ${ocrError}
            <div class="result-actions"><a class="secondary-btn" href="${escapeHtml(result.google_spreadsheet_url)}" data-google-sheet-url="${escapeHtml(result.google_spreadsheet_url)}">Открыть таблицу заведения</a></div>
          </div>
          ${renderPipelineLogs(result.pipeline_logs)}
        `;
      }
      return `
        <div class="status-box warning">
          <div class="status-line">⚠️ Накладная сохранена для ручной проверки.</div>
          <div>Google Таблица не создана.</div>
          ${methodInfo}
          ${traceMeta}
          ${ocrError}
          ${sheetError}
        </div>
        ${renderPipelineLogs(result.pipeline_logs)}
      `;
    }

    function stopTracePolling() {
      if (tracePollTimer) {
        window.clearInterval(tracePollTimer);
        tracePollTimer = null;
      }
    }

    async function pollTrace(traceId) {
      try {
        const response = await fetch('/api/v1/invoice-review/upload-trace/' + encodeURIComponent(traceId));
        if (!response.ok) {
          return;
        }
        const data = await response.json();
        if (activeTraceId !== traceId) {
          return;
        }
        if (data.completed && data.result) {
          output.innerHTML = renderTraceResult(data.result);
        } else {
          output.innerHTML = renderPipelineLogs(data.logs || []);
        }
        if (data.completed) {
          stopTracePolling();
          setUploadButtonBusy(false);
        }
      } catch (error) {
        // Ignore transient polling errors; final upload response remains source of truth.
      }
    }

    function startTracePolling(traceId) {
      activeTraceId = traceId;
      stopTracePolling();
      pollTrace(traceId);
      tracePollTimer = window.setInterval(() => {
        pollTrace(traceId);
      }, 1200);
    }

    function formatFileSize(size) {
      if (!Number.isFinite(size) || size <= 0) {
        return 'размер не определен';
      }
      if (size < 1024 * 1024) {
        return (size / 1024).toFixed(1) + ' КБ';
      }
      return (size / (1024 * 1024)).toFixed(2) + ' МБ';
    }

    function getFileKey(file) {
      return [file.name, file.size, file.lastModified, file.type].join('::');
    }

    function syncFileInputFromSelection() {
      if (typeof DataTransfer === 'undefined') {
        return;
      }
      const transfer = new DataTransfer();
      selectedFiles.forEach(file => transfer.items.add(file));
      fileInput.files = transfer.files;
    }

    function renderUploadSelection() {
      syncFileInputFromSelection();
      renderSelectedFiles();
    }

    function resetSelectedFiles() {
      selectedFiles = [];
      duplicateHint.textContent = '';
      fileInput.value = '';
      renderUploadSelection();
    }

    function handleSelectedFiles(files) {
      duplicateHint.textContent = '';
      if (!files.length) {
        renderUploadSelection();
        return;
      }
      if (!multipageCheckbox.checked) {
        selectedFiles = [files[0]];
        if (files.length > 1) {
          duplicateHint.textContent = 'Для одностраничной накладной выбран только первый файл.';
        }
        renderUploadSelection();
        return;
      }
      const existingKeys = new Set(selectedFiles.map(getFileKey));
      const skippedNames = [];
      for (const file of files) {
        const key = getFileKey(file);
        if (existingKeys.has(key)) {
          skippedNames.push(file.name);
        } else {
          selectedFiles.push(file);
          existingKeys.add(key);
        }
      }
      if (skippedNames.length) {
        duplicateHint.textContent = `Повторно выбранные файлы не добавлены: ${skippedNames.join(', ')}`;
      }
      renderUploadSelection();
    }

    function renderSelectedFiles() {
      selectedFilesBox.hidden = selectedFiles.length === 0;
      selectedFilesList.innerHTML = selectedFiles.map((file, index) => {
        const sizeText = formatFileSize(file.size);
        const pageText = `Страница ${index + 1}: `;
        const fileText = `${pageText}${file.name}`;
        return `
          <li>
            <span class="selected-file-name">
              ${escapeHtml(fileText)}
              ${sizeText ? `<span class="file-size">(${escapeHtml(sizeText)})</span>` : ''}
            </span>
            <span class="selected-file-actions">
              <button class="move-file-btn" type="button" data-move-file-index="${index}" data-move-file-direction="-1" ${index === 0 ? 'disabled' : ''}>↑</button>
              <button class="move-file-btn" type="button" data-move-file-index="${index}" data-move-file-direction="1" ${index === selectedFiles.length - 1 ? 'disabled' : ''}>↓</button>
              <button class="remove-file-btn" type="button" data-remove-file-index="${index}">Удалить</button>
            </span>
          </li>
        `;
      }).join('');
    }

    function moveSelectedFile(index, direction) {
      const nextIndex = index + direction;
      if (nextIndex < 0 || nextIndex >= selectedFiles.length) {
        return;
      }
      const copy = selectedFiles.slice();
      const [file] = copy.splice(index, 1);
      copy.splice(nextIndex, 0, file);
      selectedFiles = copy;
      duplicateHint.textContent = '';
      renderUploadSelection();
    }

    function removeSelectedFile(index) {
      selectedFiles = selectedFiles.filter((_, current) => current !== index);
      duplicateHint.textContent = '';
      renderUploadSelection();
    }

    async function refreshGoogleAuthStatus() {
      googleAuthStatus.textContent = 'Проверяю статус Google OAuth...';
      googleAuthBtn.disabled = true;
      googleAuthRefreshBtn.disabled = true;
      try {
        const response = await fetch('/api/v1/google-oauth/status');
        const data = await response.json();
        if (!response.ok) {
          throw new Error(data.detail || 'Не удалось получить статус Google OAuth.');
        }
        if (data.authorized) {
          googleAuthStatus.innerHTML = 'Google OAuth подключен. Redirect URI: <b>' + escapeHtml(data.redirect_uri || '') + '</b>';
        } else if (data.configured) {
          googleAuthStatus.innerHTML = 'Google OAuth еще не выполнен. Конфигурация есть, можно авторизоваться с этой страницы.';
        } else {
          googleAuthStatus.innerHTML = 'Google OAuth не настроен в `.env`. Сначала заполните клиентские переменные OAuth.';
        }
      } catch (error) {
        googleAuthStatus.innerHTML = 'Ошибка проверки Google OAuth: ' + escapeHtml(error.message);
      } finally {
        googleAuthBtn.disabled = false;
        googleAuthRefreshBtn.disabled = false;
      }
    }

    fileInput.addEventListener('change', () => {
      handleSelectedFiles(Array.from(fileInput.files || []));
      fileInput.value = '';
      output.innerHTML = '';
    });

    selectedFilesList.addEventListener('click', (event) => {
      const moveButton = event.target.closest('[data-move-file-index]');
      if (moveButton) {
        moveSelectedFile(
          Number(moveButton.getAttribute('data-move-file-index')),
          Number(moveButton.getAttribute('data-move-file-direction'))
        );
        return;
      }

      const removeButton = event.target.closest('[data-remove-file-index]');
      if (removeButton) {
        removeSelectedFile(Number(removeButton.getAttribute('data-remove-file-index')));
      }
    });

    multipageCheckbox.addEventListener('change', () => {
      fileInput.multiple = multipageCheckbox.checked;
      if (multipageCheckbox.checked) {
        fileInput.removeAttribute('capture');
      } else {
        fileInput.setAttribute('capture', 'environment');
      }
      resetSelectedFiles();
      output.innerHTML = '';
      fileHint.textContent = multipageCheckbox.checked
        ? 'Выбирайте файлы страниц одной накладной в правильном порядке. Повторно выбранный файл не будет добавлен и загружен.'
        : 'Если накладная одностраничная, выберите один файл.';
    });

    googleAuthBtn.addEventListener('click', () => {
      const popup = window.open(
        '/api/v1/google-oauth/authorize',
        'googleOAuth',
        'width=720,height=760,menubar=no,toolbar=no,status=no'
      );
      if (!popup) {
        googleAuthStatus.innerHTML = 'Браузер заблокировал popup. Откройте <a href="/api/v1/google-oauth/authorize" target="_blank" rel="noopener">авторизацию Google</a> вручную.';
      }
    });

    googleAuthRefreshBtn.addEventListener('click', () => {
      refreshGoogleAuthStatus();
    });

    window.addEventListener('message', event => {
      if (!event || !event.data || event.data.type !== 'google-oauth-success') {
        return;
      }
      refreshGoogleAuthStatus();
    });

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (uploadInProgress) {
        return;
      }
      const selectedMethod = extractionMethodInput.value || 'hybrid';
      const selectedMethodLabel = methodLabels[selectedMethod] || selectedMethod;
      const isMultipage = multipageCheckbox.checked;
      const formData = new FormData();
      if (!selectedFiles.length) {
        output.innerHTML = '<div class="error"><b>Ошибка:</b> выберите файл накладной.</div>';
        setUploadButtonBusy(false);
        return;
      }
      if (!isMultipage && selectedFiles.length > 1) {
        output.innerHTML = '<div class="error"><b>Ошибка:</b> для одностраничной накладной выберите один файл или включите галочку «Многостраничная накладная».</div>';
        setUploadButtonBusy(false);
        return;
      }
      if (isMultipage && selectedFiles.length > 1 && selectedMethod !== 'openai') {
        output.innerHTML = '<div class="error"><b>Ошибка:</b> многостраничная загрузка доступна только в режиме OpenAI vision parser.</div>';
        setUploadButtonBusy(false);
        return;
      }
      const traceId = (window.crypto && window.crypto.randomUUID)
        ? window.crypto.randomUUID()
        : ('trace-' + Date.now() + '-' + Math.random().toString(16).slice(2));
      let backgroundJobAccepted = false;
      startTracePolling(traceId);
      output.innerHTML = '';
      setUploadButtonBusy(true);

      selectedFiles.forEach(file => formData.append('files', file));
      formData.append('multipage_invoice', isMultipage ? 'true' : 'false');
      formData.append('create_google_sheet', 'true');
      formData.append('extraction_method', selectedMethod);
      formData.append('upload_trace_id', traceId);
      formData.append('user_timezone', getUserTimezone());
      formData.append('user_utc_offset_minutes', getUserUtcOffsetMinutes());

      try {
        const response = await fetch('/api/v1/invoice-review/upload-document-live', {
          method: 'POST',
          body: formData
        });
        const responseText = await response.text();
        let data = {};
        try {
          data = responseText ? JSON.parse(responseText) : {};
        } catch (parseError) {
          throw new Error(responseText || 'Сервер вернул не JSON-ответ.');
        }
        if (!response.ok) {
          const detail = typeof data.detail === 'object' ? JSON.stringify(data.detail) : (data.detail || JSON.stringify(data, null, 2));
          throw new Error(detail);
        }
        if (data && data.trace_id) {
          backgroundJobAccepted = true;
          output.innerHTML = '';
          return;
        }
        stopTracePolling();
        output.innerHTML = renderTraceResult(data);
      } catch (error) {
        stopTracePolling();
        let detail = null;
        try {
          detail = JSON.parse(error.message);
        } catch (parseError) {
          detail = null;
        }
        if (detail && typeof detail === 'object') {
          const retryButton = detail.retry_recommended_method
            ? `<div class="button-row compact"><button id="retryRecommendedBtn" type="button">Выбрать режим: ${escapeHtml(detail.retry_recommended_label || detail.retry_recommended_method)}</button></div>`
            : '';
          output.innerHTML = `
            <div class="error"><b>Ошибка загрузки:</b> ${escapeHtml(detail.error_message || 'Процесс остановлен из-за пустого или некорректного ответа.')}</div>
            ${retryButton}
            ${renderPipelineLogs(detail.pipeline_logs)}
          `;
          const retryButtonNode = document.getElementById('retryRecommendedBtn');
          if (retryButtonNode) {
            retryButtonNode.addEventListener('click', () => {
              extractionMethodInput.value = detail.retry_recommended_method;
            });
          }
        } else {
          output.innerHTML = `<div class="error"><b>Ошибка загрузки:</b> ${escapeHtml(error.message)}</div>`;
        }
      } finally {
        if (!backgroundJobAccepted) {
          setUploadButtonBusy(false);
        }
      }
    });

    refreshGoogleAuthStatus();
  </script>
</body>
</html>
    """)


def _fallback_ocr_result(exc: Exception) -> dict:
    return {
        "provider": "manual_review_fallback",
        "raw_text": "",
        "confidence": None,
        "pages": 0,
        "error": str(exc),
    }

@router.post("/upload-photo", response_model=InvoiceReviewResponse)
async def upload_invoice_photo_real_ocr(
    file: UploadFile = File(...),
    venue: str | None = Form(default=None),
    delivery_address: str | None = Form(default=None),
    request_id: str | None = Form(default=None),
    chat_id: str | None = Form(default=None),
    user_id: str | None = Form(default=None),
    user_timezone: str | None = Form(default=None),
    user_utc_offset_minutes: str | None = Form(default=None),
    create_google_sheet: bool = Form(default=True),
    extraction_method: str | None = Form(default=None),
    upload_trace_id: str | None = Form(default=None),
    public_api_base_url: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    target_dir = Path(settings.uploaded_invoices_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "invoice_upload").name
    file_path = target_dir / safe_name
    with file_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    return _process_invoice_upload(
        file_path=str(file_path),
        file_name=safe_name,
        file_type=file.content_type or "image",
        venue=venue,
        delivery_address=delivery_address,
        request_id=request_id,
        chat_id=chat_id,
        user_id=user_id,
        user_timezone=user_timezone,
        user_utc_offset_minutes=user_utc_offset_minutes,
        create_google_sheet=create_google_sheet,
        extraction_method=extraction_method,
        public_api_base_url=public_api_base_url,
        db=db,
        upload_trace_id=upload_trace_id,
    )


@router.post("/upload-photo-live")
async def upload_invoice_photo_live(
    file: UploadFile = File(...),
    venue: str | None = Form(default=None),
    delivery_address: str | None = Form(default=None),
    request_id: str | None = Form(default=None),
    chat_id: str | None = Form(default=None),
    user_id: str | None = Form(default=None),
    user_timezone: str | None = Form(default=None),
    user_utc_offset_minutes: str | None = Form(default=None),
    create_google_sheet: bool = Form(default=True),
    extraction_method: str | None = Form(default=None),
    upload_trace_id: str | None = Form(default=None),
    public_api_base_url: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    target_dir = Path(settings.uploaded_invoices_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "invoice_upload").name
    file_path = target_dir / safe_name
    with file_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    trace_id = upload_trace_id or f"trace-{safe_name}-{Path(file_path).stat().st_mtime_ns}"
    initialize_trace(trace_id)
    append_trace_log(
        trace_id,
        {
            "stage": "job_queued",
            "status": "running",
            "message": "Документ принят в фоновую обработку.",
            "details": {"filename": safe_name},
        },
    )
    thread = Thread(
        target=_process_invoice_upload_background,
        kwargs={
            "trace_id": trace_id,
            "file_path": str(file_path),
            "file_name": safe_name,
            "file_type": file.content_type or "image",
            "venue": venue,
            "delivery_address": delivery_address,
            "request_id": request_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "user_timezone": user_timezone,
            "user_utc_offset_minutes": user_utc_offset_minutes,
            "create_google_sheet": create_google_sheet,
            "extraction_method": extraction_method,
            "public_api_base_url": public_api_base_url or settings.public_api_base_url,
            "session_factory": _session_factory_for_db(db),
        },
        daemon=True,
    )
    thread.start()
    return {"trace_id": trace_id, "status": "processing"}


@router.post("/upload-document-live")
async def upload_invoice_document_live(
    files: list[UploadFile] = File(...),
    venue: str | None = Form(default=None),
    delivery_address: str | None = Form(default=None),
    request_id: str | None = Form(default=None),
    chat_id: str | None = Form(default=None),
    user_id: str | None = Form(default=None),
    multipage_invoice: bool = Form(default=False),
    user_timezone: str | None = Form(default=None),
    user_utc_offset_minutes: str | None = Form(default=None),
    create_google_sheet: bool = Form(default=True),
    extraction_method: str | None = Form(default=None),
    upload_trace_id: str | None = Form(default=None),
    public_api_base_url: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    if not files:
        raise HTTPException(status_code=422, detail="Нужно загрузить хотя бы одну страницу.")
    multipage_invoice = multipage_invoice or len(files) > 1
    if len(files) > settings.openai_max_image_pages:
        raise HTTPException(
            status_code=422,
            detail=f"Слишком много страниц: максимум {settings.openai_max_image_pages}.",
        )
    document_upload_id = f"upload-{uuid4().hex}"
    target_dir = Path(settings.uploaded_invoices_dir) / document_upload_id
    target_dir.mkdir(parents=True, exist_ok=True)
    file_paths: list[str] = []
    file_names: list[str] = []
    file_types: list[str] = []
    for index, file in enumerate(files, start=1):
        safe_name = Path(file.filename or f"page-{index}").name
        target = target_dir / f"{index:03d}-{safe_name}"
        with target.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        file_paths.append(str(target))
        file_names.append(safe_name)
        file_types.append(file.content_type or "image")

    trace_id = upload_trace_id or f"trace-{document_upload_id}"
    initialize_trace(trace_id)
    append_trace_log(
        trace_id,
        {
            "stage": "job_queued",
            "status": "running",
            "message": "Документ принят в фоновую обработку.",
            "details": {
                "logical_upload_id": document_upload_id,
                "pages": len(file_paths),
                "filenames": file_names,
            },
        },
    )
    thread = Thread(
        target=_process_invoice_upload_background,
        kwargs={
            "trace_id": trace_id,
            "file_path": file_paths[0],
            "file_name": file_names[0],
            "file_type": "multipage" if len(file_paths) > 1 else file_types[0],
            "file_paths": file_paths,
            "file_names": file_names,
            "file_types": file_types,
            "venue": venue,
            "delivery_address": delivery_address,
            "request_id": request_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "user_timezone": user_timezone,
            "user_utc_offset_minutes": user_utc_offset_minutes,
            "create_google_sheet": create_google_sheet,
            "extraction_method": extraction_method,
            "public_api_base_url": public_api_base_url or settings.public_api_base_url,
            "session_factory": _session_factory_for_db(db),
        },
        daemon=True,
    )
    thread.start()
    return {
        "trace_id": trace_id,
        "status": "processing",
        "logical_upload_id": document_upload_id,
        "pages": len(file_paths),
    }


@router.get("/upload-trace/{trace_id}")
def get_upload_trace(trace_id: str):
    trace = get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Трассировка загрузки не найдена")
    return trace


@router.post(
    "/bot/upload-document-live",
    response_model=BotUploadAcceptedResponse,
    dependencies=[Depends(require_bot_api_key)],
)
async def upload_invoice_document_via_bot(
    files: list[UploadFile] = File(...),
    source_channel: str = Form(default="telegram_bot"),
    document_kind: str = Form(default="primary_document"),
    source_user_id: str = Form(...),
    source_username: str | None = Form(default=None),
    source_chat_id: str | None = Form(default=None),
    organization_name: str | None = Form(default=None),
    point_name: str | None = Form(default=None),
    create_google_sheet: bool = Form(default=True),
    extraction_method: str | None = Form(default=None),
    public_api_base_url: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    if not files:
        raise HTTPException(status_code=422, detail="Нужно загрузить хотя бы один файл.")
    if len(files) > settings.openai_max_image_pages:
        raise HTTPException(
            status_code=422,
            detail=f"Слишком много файлов: максимум {settings.openai_max_image_pages}.",
        )

    document_upload_id = f"bot-source-{uuid4().hex}"
    target_dir = Path(settings.uploaded_invoices_dir) / document_upload_id
    target_dir.mkdir(parents=True, exist_ok=True)
    file_paths: list[str] = []
    file_names: list[str] = []
    file_types: list[str] = []
    unsupported_reason: str | None = None
    for index, file in enumerate(files, start=1):
        safe_name = Path(file.filename or f"file-{index}").name
        target = target_dir / f"{index:03d}-{safe_name}"
        with target.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        if target.stat().st_size == 0:
            raise HTTPException(status_code=422, detail=f"Файл {safe_name} пустой.")
        if target.stat().st_size > settings.bot_upload_max_file_bytes:
            raise HTTPException(
                status_code=422,
                detail=f"Файл {safe_name} превышает лимит {settings.bot_upload_max_file_bytes} байт.",
            )
        file_paths.append(str(target))
        file_names.append(safe_name)
        file_types.append(file.content_type or "application/octet-stream")
        supported_now, reason, _ = classify_bot_file(
            safe_name,
            file.content_type,
            document_kind=document_kind,
        )
        if not supported_now and unsupported_reason is None:
            unsupported_reason = reason

    primary_name = file_names[0]
    primary_type = "multipage" if len(file_types) > 1 else file_types[0]
    if unsupported_reason:
        upload = create_upload_journal(
            db,
            source_channel=source_channel,
            document_kind=document_kind,
            user_id=source_user_id,
            username=source_username,
            chat_id=source_chat_id,
            organization_name=organization_name,
            point_name=point_name,
            original_filename=primary_name,
            file_type=primary_type,
            raw_file_path=str(target_dir),
            files_count=len(file_paths),
            trace_id=None,
            status="unsupported_format",
            error_text=unsupported_reason,
        )
        return BotUploadAcceptedResponse(
            upload_id=upload.upload_id,
            trace_id=None,
            status="unsupported_format",
            message="Файл получен, но этот формат пока не поддерживается.",
            source_channel=source_channel,
            document_kind=document_kind,
            files_count=len(file_paths),
            unsupported_reason=unsupported_reason,
        )

    upload = create_upload_journal(
        db,
        source_channel=source_channel,
        document_kind=document_kind,
        user_id=source_user_id,
        username=source_username,
        chat_id=source_chat_id,
        organization_name=organization_name,
        point_name=point_name,
        original_filename=primary_name,
        file_type=primary_type,
        raw_file_path=str(target_dir),
        files_count=len(file_paths),
        trace_id=None,
        status="accepted_for_processing",
    )
    return _start_bot_processing(
        db,
        upload,
        file_paths=file_paths,
        file_names=file_names,
        file_types=file_types,
        create_google_sheet=create_google_sheet,
        extraction_method=extraction_method,
        public_api_base_url=public_api_base_url,
    )


@router.post(
    "/bot/drafts/pages",
    response_model=BotDraftPageResponse,
    dependencies=[Depends(require_bot_api_key)],
)
async def append_bot_draft_page(
    file: UploadFile = File(...),
    chat_id: str = Form(...),
    source_user_id: str = Form(...),
    source_username: str | None = Form(default=None),
    document_kind: str = Form(default="primary_document"),
    organization_name: str | None = Form(default=None),
    point_name: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    safe_name = Path(file.filename or "file").name
    supported_now, reason, _ = classify_bot_file(safe_name, file.content_type, document_kind=document_kind)
    if not supported_now:
        return BotDraftPageResponse(
            upload_id="",
            status="unsupported_format",
            message=reason or "Формат файла пока не поддерживается.",
            pages_count=0,
            unsupported_reason=reason,
        )

    upload = get_active_draft(db, chat_id)
    if upload is None:
        document_upload_id = f"bot-draft-{uuid4().hex}"
        target_dir = Path(settings.uploaded_invoices_dir) / document_upload_id
        target_dir.mkdir(parents=True, exist_ok=True)
        upload = create_upload_journal(
            db,
            source_channel="telegram_bot",
            document_kind=document_kind,
            user_id=source_user_id,
            username=source_username,
            chat_id=chat_id,
            organization_name=organization_name,
            point_name=point_name,
            original_filename=safe_name,
            file_type=file.content_type or "application/octet-stream",
            raw_file_path=str(target_dir),
            files_count=0,
            trace_id=None,
            status=DRAFT_STATUS,
        )
    if upload.files_count >= settings.openai_max_image_pages:
        raise HTTPException(
            status_code=422,
            detail=f"Слишком много страниц в черновике: максимум {settings.openai_max_image_pages}.",
        )

    target_dir = Path(upload.raw_file_path)
    index = upload.files_count + 1
    target = target_dir / f"{index:03d}-{safe_name}"
    with target.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    if target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=f"Файл {safe_name} пустой.")
    if target.stat().st_size > settings.bot_upload_max_file_bytes:
        target.unlink(missing_ok=True)
        raise HTTPException(
            status_code=422,
            detail=f"Файл {safe_name} превышает лимит {settings.bot_upload_max_file_bytes} байт.",
        )

    upload = append_draft_file(db, upload, filename=safe_name)
    return BotDraftPageResponse(
        upload_id=upload.upload_id,
        status=DRAFT_STATUS,
        message=f"Страница {upload.files_count} добавлена.",
        pages_count=upload.files_count,
        filenames=[draft_display_name(path) for path in list_draft_files(upload)],
    )


@router.get(
    "/bot/drafts/status",
    response_model=BotDraftStatusResponse,
    dependencies=[Depends(require_bot_api_key)],
)
def get_bot_draft_status(chat_id: str = Query(...), db: Session = Depends(get_db)):
    upload = get_active_draft(db, chat_id)
    if upload is None:
        return BotDraftStatusResponse(draft=None)
    return BotDraftStatusResponse(
        draft=BotDraftInfo(
            upload_id=upload.upload_id,
            pages_count=upload.files_count,
            filenames=[draft_display_name(path) for path in list_draft_files(upload)],
            organization_name=upload.organization_name,
            point_name=upload.point_name,
        )
    )


@router.post(
    "/bot/drafts/reset",
    response_model=BotDraftResetResponse,
    dependencies=[Depends(require_bot_api_key)],
)
def reset_bot_draft(chat_id: str = Form(...), db: Session = Depends(get_db)):
    upload = get_active_draft(db, chat_id)
    if upload is None:
        return BotDraftResetResponse(status="no_active_draft", message="Черновика еще нет.")
    delete_draft(db, upload)
    return BotDraftResetResponse(status="reset", message="Черновик очищен.")


@router.post(
    "/bot/drafts/finalize",
    response_model=BotUploadAcceptedResponse,
    dependencies=[Depends(require_bot_api_key)],
)
def finalize_bot_draft(
    chat_id: str = Form(...),
    create_google_sheet: bool = Form(default=True),
    extraction_method: str | None = Form(default=None),
    public_api_base_url: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    upload = get_active_draft(db, chat_id)
    if upload is None or upload.files_count == 0:
        raise HTTPException(status_code=422, detail="Сначала пришлите хотя бы одну страницу документа.")
    files = list_draft_files(upload)
    file_paths = [str(path) for path in files]
    file_names = [draft_display_name(path) for path in files]
    file_types = [mimetypes.guess_type(path.name)[0] or "application/octet-stream" for path in files]
    return _start_bot_processing(
        db,
        upload,
        file_paths=file_paths,
        file_names=file_names,
        file_types=file_types,
        create_google_sheet=create_google_sheet,
        extraction_method=extraction_method,
        public_api_base_url=public_api_base_url,
    )


@router.get(
    "/bot/uploads/latest",
    response_model=BotUploadStatusResponse,
    dependencies=[Depends(require_bot_api_key)],
)
def get_latest_bot_upload_status(chat_id: str = Query(...), db: Session = Depends(get_db)):
    upload = get_latest_upload_for_chat(db, chat_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="Для этого чата еще нет завершенных загрузок.")
    return _build_bot_upload_status_response(db, upload)


@router.get(
    "/bot/uploads/{upload_id}",
    response_model=BotUploadStatusResponse,
    dependencies=[Depends(require_bot_api_key)],
)
def get_bot_upload_status(upload_id: str, db: Session = Depends(get_db)):
    try:
        upload = get_upload_journal(db, upload_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _build_bot_upload_status_response(db, upload)


def _session_factory_for_db(db: Session):
    return sessionmaker(
        bind=db.get_bind(),
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )



def _start_bot_processing(
    db: Session,
    upload,
    *,
    file_paths: list[str],
    file_names: list[str],
    file_types: list[str],
    create_google_sheet: bool,
    extraction_method: str | None,
    public_api_base_url: str | None,
) -> BotUploadAcceptedResponse:
    trace_id = f"trace-{upload.upload_id}"
    update_upload_journal(db, upload.upload_id, status="accepted_for_processing", trace_id=trace_id)
    initialize_trace(trace_id)
    set_trace_metadata(
        trace_id,
        upload_id=upload.upload_id,
        source_channel=upload.source_channel,
        document_kind=upload.document_kind,
    )
    append_trace_log(
        trace_id,
        {
            "stage": "job_queued",
            "status": "running",
            "message": "Документ принят в обработку от бота.",
            "details": {
                "upload_id": upload.upload_id,
                "pages": len(file_paths),
                "filenames": file_names,
            },
        },
    )
    thread = Thread(
        target=_process_bot_upload_background,
        kwargs={
            "trace_id": trace_id,
            "upload_id": upload.upload_id,
            "file_paths": file_paths,
            "file_names": file_names,
            "file_types": file_types,
            "create_google_sheet": create_google_sheet,
            "extraction_method": extraction_method,
            "public_api_base_url": public_api_base_url or settings.public_api_base_url,
            "session_factory": _session_factory_for_db(db),
        },
        daemon=True,
    )
    thread.start()
    return BotUploadAcceptedResponse(
        upload_id=upload.upload_id,
        trace_id=trace_id,
        status="accepted_for_processing",
        message="Документ принят в обработку.",
        source_channel=upload.source_channel,
        document_kind=upload.document_kind,
        files_count=len(file_paths),
    )


def _build_bot_upload_status_response(db: Session, upload) -> BotUploadStatusResponse:
    trace = get_trace(upload.trace_id) if upload.trace_id else None
    response = trace.get("result") if trace else None
    if (
        trace
        and trace.get("completed")
        and response
        and upload.status in {"accepted_for_processing", "processing"}
    ):
        response_review_id = response.get("review_id")
        response_receiving = db.get(Receiving, response_review_id) if response_review_id else None
        recovered_status, _, _, _ = derive_bot_result(response, receiving=response_receiving)
        upload = update_upload_journal(
            db,
            upload.upload_id,
            status=recovered_status,
            error_text=response.get("google_spreadsheet_error"),
            trace_id=upload.trace_id,
            review_id=response_review_id,
        )
    receiving = db.get(Receiving, upload.review_id) if upload.review_id else None
    result_code = None
    review_status = None
    duplicate = False
    next_actions = {}
    document_summary = None
    google_spreadsheet_url = None
    google_spreadsheet_error = upload.error_text
    if response:
        result_code, _, duplicate, review_status = derive_bot_result(response, receiving=receiving)
        next_actions = build_bot_next_actions(response)
        google_spreadsheet_url = response.get("google_spreadsheet_url")
        google_spreadsheet_error = response.get("google_spreadsheet_error") or google_spreadsheet_error
    if receiving:
        document_summary = build_bot_document_summary(receiving)
    completed_statuses = {
        "unsupported_format",
        "processing_error",
        "processed",
        "transferred_to_review",
        "requires_review",
        "possible_duplicate",
    }
    return BotUploadStatusResponse(
        upload_id=upload.upload_id,
        trace_id=upload.trace_id,
        status=upload.status,
        message=_bot_status_message(upload.status, upload.error_text),
        completed=bool(trace["completed"]) if trace else upload.status in completed_statuses,
        source_channel=upload.source_channel,
        document_kind=upload.document_kind,
        files_count=upload.files_count,
        original_filename=upload.original_filename,
        organization_name=upload.organization_name,
        point_name=upload.point_name,
        user_id=upload.user_id,
        username=upload.username,
        review_id=upload.review_id,
        review_status=review_status,
        result_code=result_code,
        duplicate=duplicate,
        error_text=upload.error_text,
        uploaded_at=upload.created_at.isoformat() if upload.created_at else None,
        updated_at=upload.updated_at.isoformat() if upload.updated_at else None,
        google_spreadsheet_url=google_spreadsheet_url,
        google_spreadsheet_error=google_spreadsheet_error,
        document_summary=BotDocumentSummary(**document_summary) if document_summary else None,
        pipeline_logs=[PipelineLogEntry(**log) for log in (trace.get("logs") or [])] if trace else [],
        next_actions=next_actions,
    )


def _process_invoice_upload_background(
    *,
    trace_id: str,
    file_path: str,
    file_name: str,
    file_type: str,
    file_paths: list[str] | None = None,
    file_names: list[str] | None = None,
    file_types: list[str] | None = None,
    venue: str | None,
    delivery_address: str | None,
    request_id: str | None,
    chat_id: str | None,
    user_id: str | None,
    user_timezone: str | None = None,
    user_utc_offset_minutes: str | None = None,
    create_google_sheet: bool = True,
    extraction_method: str | None,
    public_api_base_url: str,
    session_factory=None,
) -> None:
    db = (session_factory or SessionLocal)()
    try:
        response = _process_invoice_upload(
            file_path=file_path,
            file_name=file_name,
            file_type=file_type,
            file_paths=file_paths,
            file_names=file_names,
            file_types=file_types,
            venue=venue,
            delivery_address=delivery_address,
            request_id=request_id,
            chat_id=chat_id,
            user_id=user_id,
            user_timezone=user_timezone,
            user_utc_offset_minutes=user_utc_offset_minutes,
            create_google_sheet=create_google_sheet,
            extraction_method=extraction_method,
            public_api_base_url=public_api_base_url,
            db=db,
            upload_trace_id=trace_id,
        )
        set_trace_result(trace_id, response)
        finalize_trace(trace_id, error_message=response.get("google_spreadsheet_error"))
    except Exception as exc:  # noqa: BLE001 - background upload must surface fatal failures in trace
        db_hint = describe_database_write_error(exc)
        error_message = db_hint or str(exc)
        append_trace_log(
            trace_id,
            {
                "stage": "job_failed",
                "status": "error",
                "message": "Фоновая обработка документа завершилась ошибкой.",
                "details": {"error": error_message},
            },
        )
        finalize_trace(trace_id, error_message=error_message)
    finally:
        db.close()


def _process_bot_upload_background(
    *,
    trace_id: str,
    upload_id: str,
    file_paths: list[str],
    file_names: list[str],
    file_types: list[str],
    create_google_sheet: bool,
    extraction_method: str | None,
    public_api_base_url: str,
    session_factory=None,
) -> None:
    db = (session_factory or SessionLocal)()
    try:
        upload = get_upload_journal(db, upload_id)
        update_upload_journal(db, upload_id, status="processing", error_text=None, trace_id=trace_id)
        source_metadata = build_source_metadata(upload)
        response = _process_invoice_upload(
            file_path=file_paths[0],
            file_name=file_names[0],
            file_type="multipage" if len(file_paths) > 1 else file_types[0],
            file_paths=file_paths,
            file_names=file_names,
            file_types=file_types,
            venue=upload.point_name or upload.organization_name,
            delivery_address=None,
            request_id=upload.upload_id,
            chat_id=upload.chat_id,
            user_id=upload.user_id,
            create_google_sheet=create_google_sheet,
            extraction_method=extraction_method,
            public_api_base_url=public_api_base_url,
            db=db,
            upload_trace_id=trace_id,
            source_metadata=source_metadata,
        )
        review_id = response.get("review_id")
        receiving = db.get(Receiving, review_id) if review_id else None
        final_status, _, _, _ = derive_bot_result(response, receiving=receiving)
        update_upload_journal(
            db,
            upload_id,
            status=final_status,
            error_text=response.get("google_spreadsheet_error"),
            trace_id=trace_id,
            review_id=review_id,
        )
    except HTTPException as exc:
        error_detail = exc.detail
        if isinstance(error_detail, dict):
            error_message = error_detail.get("error_message") or str(error_detail)
        else:
            error_message = str(error_detail)
        append_trace_log(
            trace_id,
            {
                "stage": "job_failed",
                "status": "error",
                "message": "Обработка документа для бота завершилась ошибкой.",
                "details": {"error": error_message},
            },
        )
        update_upload_journal(db, upload_id, status="processing_error", error_text=error_message, trace_id=trace_id)
        finalize_trace(trace_id, error_message=error_message)
    except Exception as exc:  # noqa: BLE001
        db_hint = describe_database_write_error(exc)
        error_message = db_hint or str(exc)
        append_trace_log(
            trace_id,
            {
                "stage": "job_failed",
                "status": "error",
                "message": "Обработка документа для бота завершилась ошибкой.",
                "details": {"error": error_message},
            },
        )
        update_upload_journal(db, upload_id, status="processing_error", error_text=error_message, trace_id=trace_id)
        finalize_trace(trace_id, error_message=error_message)
    finally:
        db.close()


def _process_invoice_upload(
    *,
    file_path: str,
    file_name: str,
    file_type: str,
    file_paths: list[str] | None = None,
    file_names: list[str] | None = None,
    file_types: list[str] | None = None,
    venue: str | None,
    delivery_address: str | None,
    request_id: str | None,
    chat_id: str | None,
    user_id: str | None,
    user_timezone: str | None = None,
    user_utc_offset_minutes: str | None = None,
    create_google_sheet: bool = True,
    extraction_method: str | None,
    public_api_base_url: str | None,
    db: Session,
    upload_trace_id: str | None,
    source_metadata: dict | None = None,
) -> dict:
    def trace_log(log: dict) -> None:
        if upload_trace_id:
            append_trace_log(upload_trace_id, log)

    source_paths = file_paths or [file_path]
    source_names = file_names or [file_name]
    if len(source_paths) == 1:
        extraction = extract_invoice_document(
            source_paths[0],
            source_names[0],
            extraction_method=extraction_method,
            on_log=trace_log,
        )
    else:
        extraction = extract_invoice_document_set(
            source_paths,
            source_names,
            extraction_method=extraction_method,
            on_log=trace_log,
        )
    parsed = extraction["payload"]
    pipeline_logs = extraction.get("pipeline_logs", [])
    evidence = extraction.get("evidence") or {}
    trace_metadata = {
        "logical_document_id": evidence.get("logical_document_id"),
        "evidence_version": evidence.get("evidence_version"),
        "pages": evidence.get("pages"),
        "selected_method": extraction.get("selected_method"),
        "source_files": [
            {"page_number": index, "filename": name}
            for index, name in enumerate(source_names, start=1)
        ],
    }
    if upload_trace_id:
        set_trace_metadata(upload_trace_id, **trace_metadata)
    if extraction.get("stop_recommended"):
        if upload_trace_id:
            finalize_trace(upload_trace_id, error_message=extraction.get("error"))
        raise HTTPException(
            status_code=422,
            detail={
                "error_message": extraction.get("error")
                or "Процесс остановлен: получен пустой или невалидный результат на одном из этапов.",
                "pipeline_logs": pipeline_logs,
                "retry_recommended_method": extraction.get("retry_recommended_method"),
                "retry_recommended_label": extraction.get("retry_recommended_label"),
            },
        )
    if settings.google_sheets_enabled and settings.google_target_spreadsheet_id:
        mapping_log = {
            "stage": "reference_mapping_start",
            "status": "running",
            "message": "Запускаю deterministic сопоставление со справочниками Google Sheets.",
            "details": {},
        }
        pipeline_logs.append(mapping_log)
        trace_log(mapping_log)
        try:
            references = load_invoice_reference_catalogs()
            parsed = apply_reference_mapping_to_payload(
                parsed,
                products=references["products"],
                packages=references["packages"],
                conversion_exceptions=references.get("conversion_exceptions") or [],
            )
            mapping_complete_log = {
                "stage": "reference_mapping_complete",
                "status": "ok",
                "message": "Сопоставление со справочниками Google Sheets завершено.",
                "details": {"items_count": len(parsed.get("items") or [])},
            }
            pipeline_logs.append(mapping_complete_log)
            trace_log(mapping_complete_log)
        except Exception as exc:  # noqa: BLE001 - reference outage must remain visible but not destroy OCR output
            parsed.setdefault("parser_notes", []).append(
                f"Не удалось выполнить сопоставление со справочниками Google Sheets: {exc}"
            )
            mapping_error_log = {
                "stage": "reference_mapping_failed",
                "status": "error",
                "message": "Не удалось выполнить сопоставление со справочниками Google Sheets.",
                "details": {"error": str(exc)},
            }
            pipeline_logs.append(mapping_error_log)
            trace_log(mapping_error_log)
    _apply_duplicate_status(db, parsed)
    parser_metadata = parsed.get("parser_metadata") or {}
    if source_metadata:
        parser_metadata["source_upload"] = source_metadata
    parser_metadata["source_files"] = [
        {"page_number": index, "filename": name}
        for index, name in enumerate(source_names, start=1)
    ]
    parser_metadata["evidence_version"] = evidence.get("evidence_version")
    parser_metadata["logical_document_id"] = evidence.get("logical_document_id")
    payload = InvoiceReviewCreateRequest(
        file_id="; ".join(source_names),
        file_type=file_type,
        file_url=file_path,
        raw_text=extraction.get("raw_text"),
        request_id=request_id,
        supplier=parsed.get("supplier"),
        supplier_legal_name=parsed.get("supplier_legal_name"),
        invoice_date=parsed.get("invoice_date"),
        invoice_number=parsed.get("invoice_number"),
        venue=venue or (source_metadata or {}).get("point_name") or (source_metadata or {}).get("organization_name") or parsed.get("venue"),
        delivery_address=delivery_address or parsed.get("delivery_address"),
        display_store=parsed.get("display_store") or parsed.get("store"),
        document_form=parsed.get("document_form"),
        supplier_inn=parsed.get("supplier_inn"),
        consignee=parsed.get("consignee"),
        recipient=parsed.get("recipient"),
        trade_point=parsed.get("trade_point"),
        warehouse=parsed.get("warehouse") or parsed.get("display_store") or parsed.get("store"),
        basis=parsed.get("basis"),
        total_sum=parsed.get("total_sum"),
        iiko_default_store_id=parsed.get("iiko_default_store_id") or parsed.get("store"),
        chat_id=chat_id,
        user_id=user_id,
        user_timezone=user_timezone,
        user_utc_offset_minutes=user_utc_offset_minutes or None,
        multipage_invoice=len(source_paths) > 1,
        items=[RecognizedInvoiceItem(**item) for item in parsed.get("items", [])],
        parser_metadata=parser_metadata,
    )
    receiving = create_invoice_review(db, payload)
    sheet = build_review_sheet(receiving)
    csv_path = save_review_csv(receiving)
    response = _review_response(receiving, sheet, csv_path)
    response["ocr"] = {
        "provider": extraction["provider"],
        "pages": extraction.get("pages"),
        "raw_text_length": len(extraction.get("raw_text") or ""),
        "selected_method": extraction.get("selected_method"),
        "selected_method_label": _extraction_method_label(extraction.get("selected_method")),
    }
    response["trace_metadata"] = trace_metadata
    if extraction.get("error"):
        response["ocr"]["error"] = extraction["error"]
    response["parser_notes"] = parsed.get("parser_notes", [])
    response["parser_provider"] = parsed.get("parser_provider")
    response["pipeline_logs"] = [PipelineLogEntry(**log).model_dump(mode="json") for log in pipeline_logs]
    if create_google_sheet:
        google_log = {
            "stage": "google_sheet_start",
            "status": "running",
            "message": "Пишу результат в Google Sheets.",
            "details": {},
        }
        response["pipeline_logs"].append(google_log)
        trace_log(google_log)
        try:
            spreadsheet = create_real_google_sheet_for_review(db, receiving, public_api_base_url or settings.public_api_base_url)
            response["google_spreadsheet_id"] = spreadsheet["spreadsheet_id"]
            response["google_spreadsheet_url"] = spreadsheet["spreadsheet_url"]
            google_ok_log = {
                "stage": "google_sheet_complete",
                "status": "ok",
                "message": "Google Sheets успешно обновлен.",
                "details": {"spreadsheet_id": spreadsheet["spreadsheet_id"]},
            }
            response["pipeline_logs"].append(google_ok_log)
            trace_log(google_ok_log)
        except Exception as exc:  # noqa: BLE001 - external provider errors must be surfaced to user
            response["google_spreadsheet_error"] = str(exc)
            google_error_log = {
                "stage": "google_sheet_failed",
                "status": "error",
                "message": "Не удалось записать результат в Google Sheets.",
                "details": {"error": str(exc)},
            }
            response["pipeline_logs"].append(google_error_log)
            trace_log(google_error_log)
    if upload_trace_id:
        set_trace_result(upload_trace_id, response)
        finalize_trace(upload_trace_id, error_message=response.get("google_spreadsheet_error"))
    return response


def _extraction_method_label(value: str | None) -> str | None:
    if value == "openai":
        return "OpenAI vision parser (text + images)"
    if value == "google_ocr":
        return "Google OCR"
    if value == "mineru":
        return "MinerU"
    if value == "hybrid":
        return "Гибрид: MinerU -> Google OCR fallback"
    return None


def _bot_status_message(status: str, error_text: str | None) -> str:
    if status == "collecting":
        return "Черновик документа собирается."
    if status == "accepted_for_processing":
        return "Документ принят в обработку."
    if status == "processing":
        return "Документ обрабатывается."
    if status == "transferred_to_review":
        return "Документ обработан и передан в модуль проверки данных."
    if status == "requires_review":
        return "Документ обработан, но требует проверки в модуле проверки данных."
    if status == "possible_duplicate":
        return "Документ похож на уже загруженный и требует проверки на дубль."
    if status == "unsupported_format":
        return error_text or "Формат файла пока не поддерживается."
    if status == "processing_error":
        return error_text or "Во время обработки произошла ошибка."
    if status == "processed":
        return "Документ обработан."
    return error_text or "Статус загрузки обновлен."


def _apply_duplicate_status(db: Session, parsed: dict) -> None:
    metadata = parsed.get("parser_metadata")
    if not isinstance(metadata, dict):
        return
    document_form = str(parsed.get("document_form") or "").strip()
    invoice_number = canonical_invoice_number(parsed.get("invoice_number"), document_form=document_form)
    supplier = str(parsed.get("supplier") or "").strip().casefold()
    invoice_date = str(parsed.get("invoice_date") or "").strip()
    total_sum = parsed.get("total_sum")
    if not invoice_number and not supplier:
        return

    possible = False
    for document in db.query(ReceivingDocument).all():
        existing_invoice_number = canonical_invoice_number(document.invoice_number, document_form=document_form)
        if invoice_number and existing_invoice_number == invoice_number:
            existing_supplier = (document.supplier_legal_name or "").strip().casefold()
            if supplier and existing_supplier == supplier:
                metadata.update(duplicate="Да", upload_status="Не готово", row_status="Распознано")
                return
            possible = True
        if supplier and (document.supplier_legal_name or "").strip().casefold() == supplier:
            if invoice_date and document.invoice_date == invoice_date:
                try:
                    stored = json.loads(document.recognized_items_json or "{}")
                    stored_total = (stored.get("header") or {}).get("total_sum")
                except (json.JSONDecodeError, AttributeError):
                    stored_total = None
                if total_sum is not None and stored_total == total_sum:
                    possible = True
    if possible:
        metadata.update(duplicate="?", upload_status="Требует проверки", row_status="Распознано")


@router.post("/upload", response_model=InvoiceReviewResponse)
def upload_invoice_for_review(payload: InvoiceReviewCreateRequest, db: Session = Depends(get_db)):
    receiving = create_invoice_review(db, payload)
    sheet = build_review_sheet(receiving)
    csv_path = save_review_csv(receiving)
    return _review_response(receiving, sheet, csv_path)


@router.put("/{review_id}", response_model=InvoiceReviewResponse)
def update_review(review_id: int, payload: InvoiceReviewUpdateRequest, db: Session = Depends(get_db)):
    try:
        receiving = update_invoice_review(db, review_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    sheet = build_review_sheet(receiving)
    csv_path = save_review_csv(receiving)
    return _review_response(receiving, sheet, csv_path)




@router.get("/iiko/references/status")
def get_iiko_references_status():
    return get_iiko_reference_status()


@router.post("/{review_id}/iiko-auto-map", response_model=InvoiceReviewResponse)
def auto_map_review_iiko_fields(review_id: int, force_refresh: bool = Query(default=False), db: Session = Depends(get_db)):
    try:
        receiving = remap_review_with_iiko_references(db, review_id, force_refresh=force_refresh)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    sheet = build_review_sheet(receiving)
    csv_path = save_review_csv(receiving)
    return _review_response(receiving, sheet, csv_path)


@router.get("/{review_id}/sheet")
def get_google_sheet_preview(review_id: int, db: Session = Depends(get_db)):
    receiving = _get_review(db, review_id)
    return build_review_sheet(receiving)


@router.get("/{review_id}/sheet.csv", response_class=PlainTextResponse)
def get_google_sheet_csv(review_id: int, db: Session = Depends(get_db)):
    receiving = _get_review(db, review_id)
    return PlainTextResponse(build_review_csv(receiving), media_type="text/csv; charset=utf-8")


@router.get("/{review_id}/preview")
def get_iiko_send_preview(
    review_id: int,
    target_organization: str | None = None,
    target_warehouse: str | None = None,
    db: Session = Depends(get_db),
):
    receiving = _get_review(db, review_id)
    return build_iiko_preview(receiving, target_organization, target_warehouse)


@router.get("/{review_id}/apps-script", response_class=PlainTextResponse)
def get_apps_script_sample(
    review_id: int,
    public_api_base_url: str = Query("https://YOUR_API_HOST"),
    db: Session = Depends(get_db),
):
    receiving = _get_review(db, review_id)
    return PlainTextResponse(build_apps_script_sample(receiving, public_api_base_url), media_type="text/plain")


@router.post("/{review_id}/google-sheet")
def create_google_sheet_for_review(
    review_id: int,
    public_api_base_url: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    receiving = _get_review(db, review_id)
    try:
        return create_real_google_sheet_for_review(db, receiving, public_api_base_url or settings.public_api_base_url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=str(exc)) from exc



@router.get("/{review_id}/send-page", response_class=HTMLResponse)
def open_iiko_send_page(review_id: int, db: Session = Depends(get_db)):
    receiving = _get_review(db, review_id)
    try:
        spreadsheet = get_latest_google_spreadsheet_info(db, review_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    invoice_number = html.escape(receiving.order_number or str(review_id))
    spreadsheet_url = html.escape(spreadsheet.get("spreadsheet_url") or "")
    spreadsheet_link = (
        f'<a href="{spreadsheet_url}" data-google-sheet-url="{spreadsheet_url}">Вернуться в Google Таблицу</a>'
        if spreadsheet_url
        else ""
    )
    return HTMLResponse(f"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>АвтоСнаб — отправка в iiko</title>
  <style>
    body {{ font-family: Arial, sans-serif; background: #f5f7fb; color: #111827; margin: 0; }}
    .page {{ max-width: 760px; margin: 0 auto; padding: 40px 16px; }}
    .card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 16px; padding: 28px; box-shadow: 0 10px 30px rgba(15,23,42,.08); }}
    h1 {{ margin: 0 0 12px; font-size: 28px; }}
    .muted {{ color: #6b7280; line-height: 1.5; }}
    .btn {{ margin-top: 22px; border: 0; border-radius: 12px; padding: 14px 22px; background: #2563eb; color: #fff; font-weight: 700; font-size: 16px; cursor: pointer; }}
    .btn:disabled {{ opacity: .6; cursor: not-allowed; }}
    .options {{ margin-top: 18px; line-height: 1.8; }}
    .result {{ margin-top: 22px; padding: 16px; border-radius: 12px; background: #f0fdf4; border: 1px solid #bbf7d0; white-space: pre-wrap; }}
    .error {{ margin-top: 22px; padding: 16px; border-radius: 12px; background: #fef2f2; border: 1px solid #fecaca; white-space: pre-wrap; }}
    a {{ color: #2563eb; }}
  </style>
</head>
<body>
  <main class="page">
    <section class="card">
      <h1>Отправка накладной в iiko</h1>
      <p class="muted">Накладная: <b>{invoice_number}</b></p>
      <p class="muted">Перед отправкой проверьте лист <b>«Накладные»</b>. После нажатия кнопки backend прочитает данные из Google Таблицы и отправит их в iiko.</p>
      <div>{spreadsheet_link}</div>
      <div class="options">
        <label><input id="allowWarnings" type="checkbox" checked /> Разрешить отправку с предупреждениями проверки</label><br />
        <label><input id="dryRun" type="checkbox" /> Тестовый режим без реальной отправки</label>
      </div>
      <button id="sendBtn" class="btn" type="button">Отправить в iiko</button>
      <div id="result"></div>
    </section>
  </main>
  <script>
    const btn = document.getElementById('sendBtn');
    const result = document.getElementById('result');
    const sheetWindowName = 'autosnab_google_sheet';
    let sheetWindow = null;

    function openSheetInSingleTab(url) {{
      const targetUrl = String(url || '').trim();
      if (!targetUrl) {{
        return false;
      }}

      sheetWindow = window.open(targetUrl, sheetWindowName);
      if (sheetWindow) {{
        sheetWindow.focus();
      }}

      return false;
    }}

    document.addEventListener('click', (event) => {{
      const link = event.target.closest('[data-google-sheet-url]');
      if (!link) {{
        return;
      }}

      event.preventDefault();
      openSheetInSingleTab(link.getAttribute('data-google-sheet-url'));
    }});

    btn.addEventListener('click', async () => {{
      if (!confirm('Отправить накладную в iiko?')) return;
      btn.disabled = true;
      result.innerHTML = '<div class="result">Отправка выполняется...</div>';
      const allow = document.getElementById('allowWarnings').checked;
      const dry = document.getElementById('dryRun').checked;
      try {{
        const response = await fetch(`/api/v1/invoice-review/{review_id}/send-from-google-sheet?allow_with_warnings=${{allow}}&dry_run=${{dry}}`, {{ method: 'POST' }});
        const text = await response.text();
        let data = {{}};
        try {{ data = text ? JSON.parse(text) : {{}}; }} catch (_) {{ throw new Error(text || 'Сервер вернул не JSON-ответ.'); }}
        if (!response.ok) throw new Error(data.detail || JSON.stringify(data, null, 2));
        result.innerHTML = '<div class="result">Готово. Статус: ' + data.status + '\nExport ID: ' + data.export_id + '</div>';
      }} catch (err) {{
        result.innerHTML = '<div class="error">Ошибка отправки: ' + String(err.message || err) + '</div>';
      }} finally {{
        btn.disabled = false;
      }}
    }});
  </script>
</body>
</html>
    """)


@router.post("/{review_id}/send-from-google-sheet")
def send_from_google_sheet_button(
    review_id: int,
    allow_with_warnings: bool = Query(default=True),
    dry_run: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    try:
        export = send_google_sheet_and_confirm_to_iiko(
            db,
            review_id,
            allow_with_warnings=allow_with_warnings,
            dry_run=dry_run,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - external send errors must be visible to user
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "export_id": export.id,
        "review_id": review_id,
        "status": export.status,
        "target_system": export.target_system,
        "error_message": export.error_message,
        "payload": json.loads(export.payload_json),
    }

@router.post("/{review_id}/sync-sheet-and-confirm-send")
def sync_sheet_and_confirm_send(review_id: int, payload: SyncSheetAndConfirmRequest, db: Session = Depends(get_db)):
    try:
        export = sync_sheet_and_confirm_to_iiko(db, review_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "export_id": export.id,
        "review_id": review_id,
        "status": export.status,
        "target_system": export.target_system,
        "payload": json.loads(export.payload_json),
    }


@router.post("/{review_id}/confirm-send")
def confirm_send(review_id: int, payload: ConfirmSendToIikoRequest, db: Session = Depends(get_db)):
    try:
        export = confirm_and_send_to_iiko(db, review_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "export_id": export.id,
        "review_id": review_id,
        "status": export.status,
        "target_system": export.target_system,
        "payload": json.loads(export.payload_json),
    }


@router.get("/exports/iiko")
def list_invoice_review_exports(db: Session = Depends(get_db)):
    exports = (
        db.query(AccountingExport)
        .filter(AccountingExport.target_system == "iiko")
        .order_by(AccountingExport.id.desc())
        .all()
    )
    return {
        "exports": [
            {
                "id": export.id,
                "receiving_id": export.receiving_id,
                "request_id": export.request_id,
                "order_number": export.order_number,
                "status": export.status,
                "error_message": export.error_message,
                "created_at": export.created_at.isoformat() if export.created_at else None,
            }
            for export in exports
        ]
    }


def _get_review(db: Session, review_id: int) -> Receiving:
    receiving = db.get(Receiving, review_id)
    if receiving is None:
        raise HTTPException(status_code=404, detail="Проверка накладной не найдена")
    return receiving


def _review_response(receiving: Receiving, sheet: dict, csv_path: str) -> dict:
    return {
        "review_id": receiving.id,
        "status": sheet["status"],
        "issues": sheet["issues"],
        "spreadsheet_name": sheet["spreadsheet_name"],
        "csv_path": csv_path,
        "next_actions": {
            "open_sheet": f"/api/v1/invoice-review/{receiving.id}/sheet",
            "open_csv": f"/api/v1/invoice-review/{receiving.id}/sheet.csv",
            "create_google_sheet": f"/api/v1/invoice-review/{receiving.id}/google-sheet",
            "send_page": f"/api/v1/invoice-review/{receiving.id}/send-page",
            "apps_script": f"/api/v1/invoice-review/{receiving.id}/apps-script",
            "preview": f"/api/v1/invoice-review/{receiving.id}/preview",
            "confirm_send": f"/api/v1/invoice-review/{receiving.id}/confirm-send",
            "sync_sheet_and_confirm_send": f"/api/v1/invoice-review/{receiving.id}/sync-sheet-and-confirm-send",
        },
    }
