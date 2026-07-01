from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "АвтоСнаб Backend MVP-4"
    database_url: str = "sqlite:///./autosnab_mvp.db"
    api_prefix: str = "/api/v1"

    # Google Drive OCR + Google Sheets integration.
    # Основной режим: OAuth обычного Google-пользователя.
    # Service account поля оставлены только для обратной совместимости,
    # но новые вызовы Drive OCR/Sheets используют OAuth credentials.
    google_auth_mode: str = "oauth"
    google_oauth_client_secrets_file: str = "backend/secrets/oauth-client.json"
    google_oauth_token_file: str = "backend/secrets/oauth-token.json"
    google_oauth_redirect_uri: str = "http://localhost:8000/api/v1/google-oauth/callback"

    google_application_credentials: str | None = None
    google_service_account_file: str | None = None
    google_drive_folder_id: str | None = None
    google_sheets_enabled: bool = False
    google_apps_script_enabled: bool = False
    google_drive_ocr_enabled: bool = True
    google_drive_ocr_language: str = "ru"
    google_drive_ocr_folder_id: str | None = None
    google_drive_ocr_delete_temp_files: bool = True
    public_api_base_url: str = "https://YOUR_API_HOST"
    uploaded_invoices_dir: str = "uploads/invoices"

    # iiko Server API integration for incoming invoice XML.
    iiko_integration_enabled: bool = False
    iiko_base_url: str | None = None
    iiko_login: str | None = None
    iiko_password_sha1: str | None = None
    iiko_token: str | None = None
    iiko_timeout_seconds: float = 30.0
    iiko_auto_mapping_enabled: bool = True
    iiko_mapping_min_confidence: float = 0.72

    # OCR text is structured by the built-in deterministic parser.
    # External AI parsers are not used in the current project version.

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
