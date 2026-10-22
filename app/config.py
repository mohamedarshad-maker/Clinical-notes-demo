"""Application configuration."""
import shutil
from pathlib import Path
from typing import Literal, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Application
    app_env: str = "development"
    demo_only: bool = True
    app_secret_key: str = "dev-secret-key-change-in-production"

    # Database
    database_url: str = "sqlite:///./clinical_notes.db"

    # Supabase
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    # Support newer Supabase secret key naming
    supabase_secret_key: Optional[str] = None
    supabase_storage_bucket: str = "clinical-notes-private"

    # LLM Provider
    llm_provider: Literal["gemini", "groq"] = "gemini"
    llm_model: str = "gemini-1.5-flash"
    gemini_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None
    llm_max_concurrent_requests: int = 3
    max_active_patient_analyses: int = 1
    job_lease_ttl_seconds: int = 600
    job_lease_timeout_minutes: int = 10
    ocr_worker_concurrency: int = 1

    # WHO ICD-11
    who_icd_client_id: str = ""
    who_icd_client_secret: str = ""
    who_icd_release: str = "2026-01"
    who_icd_language: str = "en"

    # Upload Limits
    max_files_per_upload: int = 5
    max_file_mb: int = 10
    max_batch_mb: int = 25
    max_pdf_pages: int = 20
    max_image_megapixels: int = 25

    # OCR
    ocr_language: str = "eng"
    tesseract_cmd: Optional[str] = None

    # Demo Access (supports both DEMO_AUTH_USERNAME and DEMO_USERNAME)
    demo_auth_username: Optional[str] = None
    demo_auth_password: Optional[str] = None
    demo_username: Optional[str] = None
    demo_password: Optional[str] = None

    @property
    def effective_demo_username(self) -> str:
        """Return configured demo username."""
        return (self.demo_auth_username or self.demo_username or "").strip()

    @property
    def effective_demo_password(self) -> str:
        """Return configured demo password."""
        return (self.demo_auth_password or self.demo_password or "").strip()

    @property
    def is_demo_auth_configured(self) -> bool:
        """Check whether HTTP Basic demo auth credentials are configured."""
        return bool(self.effective_demo_username and self.effective_demo_password)

    @property
    def max_file_bytes(self) -> int:
        """Maximum file size in bytes."""
        return self.max_file_mb * 1024 * 1024

    @property
    def max_batch_bytes(self) -> int:
        """Maximum batch size in bytes."""
        return self.max_batch_mb * 1024 * 1024

    @property
    def effective_supabase_key(self) -> str:
        """Return configured privileged Supabase server key."""
        return (self.supabase_service_role_key or self.supabase_secret_key or "").strip()

    @property
    def is_storage_configured(self) -> bool:
        """Check whether Supabase Storage credentials are configured."""
        url = (self.supabase_url or "").strip()
        key = self.effective_supabase_key
        if not url or not key:
            return False
        placeholders = {
            "https://placeholder.supabase.co",
            "https://your-project.supabase.co",
            "placeholder-key",
            "your-service-role-key",
        }
        return url not in placeholders and key not in placeholders

    @property
    def is_llm_configured(self) -> bool:
        """Check whether the selected LLM provider has an API key configured."""
        placeholders = {"", "your-gemini-api-key",
                        "your-groq-api-key", "placeholder"}
        if self.llm_provider == "gemini":
            key = (self.gemini_api_key or "").strip()
            return bool(key and key not in placeholders)
        if self.llm_provider == "groq":
            key = (self.groq_api_key or "").strip()
            return bool(key and key not in placeholders)
        return False

    @property
    def is_who_icd_configured(self) -> bool:
        """Check whether WHO ICD-11 credentials are configured."""
        cid = (self.who_icd_client_id or "").strip()
        sec = (self.who_icd_client_secret or "").strip()
        placeholders = {
            "",
            "your-client-id-from-who-registration",
            "your-client-secret-from-who-registration",
            "your-client-id",
            "your-client-secret",
            "placeholder",
        }
        return bool(cid and sec and cid not in placeholders and sec not in placeholders)

    @property
    def is_tesseract_configured(self) -> bool:
        """Check whether Tesseract executable is available."""
        if self.tesseract_cmd and Path(self.tesseract_cmd).is_file():
            return True
        if shutil.which("tesseract"):
            return True
        common_win = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
        return common_win.is_file()

    @property
    def resolved_tesseract_cmd(self) -> Optional[str]:
        """Return resolved Tesseract command path if available."""
        if self.tesseract_cmd and Path(self.tesseract_cmd).is_file():
            return self.tesseract_cmd
        which_path = shutil.which("tesseract")
        if which_path:
            return which_path
        common_win = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
        if common_win.is_file():
            return str(common_win)
        return self.tesseract_cmd


settings = Settings()
