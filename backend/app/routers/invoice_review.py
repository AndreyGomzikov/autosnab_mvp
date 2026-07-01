import html
import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.models.accounting import AccountingExport
from app.models.receiving import Receiving
from app.schemas.invoice_review import (
    ConfirmSendToIikoRequest,
    InvoiceReviewCreateRequest,
    InvoiceReviewResponse,
    InvoiceReviewUpdateRequest,
    RecognizedInvoiceItem,
    SyncSheetAndConfirmRequest,
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
from app.services.invoice_parser_service import extract_invoice_payload_with_fallback
from app.services.ocr_service import OcrConfigurationError, recognize_invoice_image

router = APIRouter(prefix="/invoice-review", tags=["invoice-review"])




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
    input[type="file"] {
      width: 100%; box-sizing: border-box; padding: 12px;
      border: 1px solid #d1d5db; border-radius: 10px; background: #fff;
      font-size: 15px;
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
  </style>
</head>
<body>
  <main class="page">
    <section class="card">
      <h1>АвтоСнаб - Загрузка накладной</h1>
      <div class="subtitle">
        Загрузите фото, скан или PDF накладной. Система распознает данные через Google Drive OCR и создаст Google Таблицу для проверки.
      </div>
      <form id="uploadForm">
        <label for="file">Файл накладной</label>
        <input id="file" name="file" type="file" accept="image/*,.pdf" capture="environment" required />
        <div class="button-row">
          <button id="submitBtn" type="submit">Загрузить накладную с таблицей заведения</button>
        </div>
      </form>
      <div id="output"></div>
    </section>
  </main>

  <script>
    const form = document.getElementById('uploadForm');
    const output = document.getElementById('output');
    const button = document.getElementById('submitBtn');

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>'"]/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
      }[ch]));
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      output.innerHTML = '<div class="loading">Файл загружается и распознается через Google Drive OCR. Подождите...</div>';
      button.disabled = true;

      const formData = new FormData();
      const fileInput = document.getElementById('file');
      if (!fileInput.files.length) {
        output.innerHTML = '<div class="error"><b>Ошибка:</b> выберите файл накладной.</div>';
        button.disabled = false;
        return;
      }
      formData.append('file', fileInput.files[0]);
      formData.append('create_google_sheet', 'true');

      try {
        const response = await fetch('/api/v1/invoice-review/upload-photo', {
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
          throw new Error(data.detail || JSON.stringify(data, null, 2));
        }

        const hasGoogleSheet = Boolean(data.google_spreadsheet_url);
        const ocrError = data.ocr && data.ocr.error ? `<div>⚠️ OCR не сработал: ${escapeHtml(data.ocr.error)}</div>` : '';
        if (hasGoogleSheet) {
          output.innerHTML = `
            <div class="status-box">
              <div class="status-line">✅ Накладная обработана успешно.</div>
              <div class="status-line">✅ Данные добавлены в таблицу заведения.</div>
              ${ocrError}
              <div class="result-actions"><a class="secondary-btn" href="${escapeHtml(data.google_spreadsheet_url)}" target="_blank" rel="noopener">Открыть таблицу заведения</a></div>
            </div>
          `;
        } else {
          const sheetError = data.google_spreadsheet_error ? `<div>Ошибка Google Таблицы: ${escapeHtml(data.google_spreadsheet_error)}</div>` : '';
          output.innerHTML = `
            <div class="status-box warning">
              <div class="status-line">⚠️ Накладная сохранена для ручной проверки.</div>
              <div>Google Таблица не создана.</div>
              ${ocrError}
              ${sheetError}
            </div>
          `;
        }
      } catch (error) {
        output.innerHTML = `<div class="error"><b>Ошибка загрузки:</b> ${escapeHtml(error.message)}</div>`;
      } finally {
        button.disabled = false;
      }
    });
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
    create_google_sheet: bool = Form(default=True),
    public_api_base_url: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    target_dir = Path(settings.uploaded_invoices_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "invoice_upload").name
    file_path = target_dir / safe_name
    with file_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        ocr_result = recognize_invoice_image(str(file_path))
    except OcrConfigurationError as exc:
        ocr_result = _fallback_ocr_result(exc)
    except Exception as exc:  # noqa: BLE001 - external OCR errors must not break upload
        ocr_result = _fallback_ocr_result(exc)

    parsed = extract_invoice_payload_with_fallback(ocr_result["raw_text"], safe_name)
    payload = InvoiceReviewCreateRequest(
        file_id=safe_name,
        file_type=file.content_type or "image",
        file_url=str(file_path),
        raw_text=ocr_result["raw_text"],
        request_id=request_id,
        supplier=parsed.get("supplier"),
        supplier_legal_name=parsed.get("supplier_legal_name"),
        invoice_date=parsed.get("invoice_date"),
        invoice_number=parsed.get("invoice_number"),
        venue=venue or parsed.get("venue"),
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
        items=[RecognizedInvoiceItem(**item) for item in parsed.get("items", [])],
    )
    receiving = create_invoice_review(db, payload)
    sheet = build_review_sheet(receiving)
    csv_path = save_review_csv(receiving)
    response = _review_response(receiving, sheet, csv_path)
    response["ocr"] = {
        "provider": ocr_result["provider"],
        "pages": ocr_result.get("pages"),
        "raw_text_length": len(ocr_result.get("raw_text") or ""),
    }
    if ocr_result.get("error"):
        response["ocr"]["error"] = ocr_result["error"]
    response["parser_notes"] = parsed.get("parser_notes", [])
    response["parser_provider"] = parsed.get("parser_provider")
    if create_google_sheet:
        try:
            spreadsheet = create_real_google_sheet_for_review(db, receiving, public_api_base_url or settings.public_api_base_url)
            response["google_spreadsheet_id"] = spreadsheet["spreadsheet_id"]
            response["google_spreadsheet_url"] = spreadsheet["spreadsheet_url"]
        except Exception as exc:  # noqa: BLE001 - external provider errors must be surfaced to user
            response["google_spreadsheet_error"] = str(exc)
    return response


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
        f'<a href="{spreadsheet_url}" target="_blank" rel="noopener">Вернуться в Google Таблицу</a>'
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
