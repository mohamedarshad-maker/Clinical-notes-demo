"""Supabase Storage service for private file management."""
import hashlib
import logging
import re
import shutil
from pathlib import Path
from typing import Optional
from uuid import UUID
from supabase import create_client, Client
from app.config import settings

logger = logging.getLogger(__name__)

LOCAL_STORAGE_ROOT = Path(".local_storage")


class StorageService:
    """Manages private file storage in Supabase with local private fallback for dev/test."""

    def __init__(self):
        """Initialize storage client."""
        self.bucket_name = settings.supabase_storage_bucket
        self._client: Optional[Client] = None

    @property
    def client(self) -> Client:
        """Lazy initialize Supabase client."""
        if self._client is None:
            if settings.is_storage_configured:
                self._client = create_client(
                    settings.supabase_url,
                    settings.effective_supabase_key
                )
            else:
                raise ValueError("Supabase credentials not configured. Please update .env file.")
        return self._client

    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename to prevent path traversal or invalid object keys."""
        base = Path(filename or "document").name
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base)
        return cleaned or "document"

    def _local_path(self, object_path: str) -> Path:
        """Resolve safe local fallback path."""
        root = (LOCAL_STORAGE_ROOT / self.bucket_name).resolve()
        target = (root / object_path).resolve()
        if not str(target).startswith(str(root)):
            raise ValueError("Invalid storage object path")
        return target

    async def ensure_bucket_exists(self) -> bool:
        """Ensure the private bucket exists."""
        if not settings.is_storage_configured:
            if settings.app_env != "production":
                (LOCAL_STORAGE_ROOT / self.bucket_name).mkdir(parents=True, exist_ok=True)
                return True
            return False

        try:
            self.client.storage.get_bucket(self.bucket_name)
            return True
        except Exception as e:
            logger.warning(f"Bucket check failed: {e}")
            try:
                self.client.storage.create_bucket(
                    self.bucket_name,
                    options={"public": False}
                )
                logger.info(f"Created private bucket: {self.bucket_name}")
                return True
            except Exception as create_error:
                logger.error(f"Failed to create bucket: {create_error}")
                return False

    def generate_object_path(self, patient_id: UUID, document_id: UUID, filename: str) -> str:
        """Generate object storage path."""
        safe_name = self._sanitize_filename(filename)
        return f"patients/{patient_id}/{document_id}/{safe_name}"

    def compute_file_hash(self, file_content: bytes) -> str:
        """Compute SHA-256 hash of file content."""
        return hashlib.sha256(file_content).hexdigest()

    async def upload_file(
        self,
        patient_id: UUID,
        document_id: UUID,
        filename: str,
        content: bytes,
        content_type: str
    ) -> dict:
        """
        Upload file to private storage.

        Returns dict with success status and object path.
        """
        object_path = self.generate_object_path(patient_id, document_id, filename)

        if settings.is_storage_configured:
            try:
                self.client.storage.from_(self.bucket_name).upload(
                    path=object_path,
                    file=content,
                    file_options={"content-type": content_type, "upsert": "true"}
                )
                logger.info(f"Uploaded file to Supabase Storage: {object_path}")
                return {
                    "success": True,
                    "object_path": object_path,
                    "bucket": self.bucket_name,
                    "storage_backend": "supabase"
                }
            except Exception as e:
                logger.error(f"Supabase upload failed for {object_path}: {e}")
                if settings.app_env == "production":
                    return {
                        "success": False,
                        "error": str(e)
                    }
                logger.warning("Falling back to local private storage in non-production mode")

        if settings.app_env == "production" and not settings.is_storage_configured:
            return {
                "success": False,
                "error": "Supabase Storage is not configured"
            }

        try:
            target = self._local_path(object_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            logger.info(f"Saved file to local private storage: {object_path}")
            return {
                "success": True,
                "object_path": object_path,
                "bucket": self.bucket_name,
                "storage_backend": "local"
            }
        except Exception as e:
            logger.error(f"Local storage fallback failed for {object_path}: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def download_file(self, object_path: str) -> Optional[bytes]:
        """Download file from storage."""
        if settings.is_storage_configured:
            try:
                result = self.client.storage.from_(self.bucket_name).download(object_path)
                if result is not None:
                    return result
            except Exception as e:
                logger.warning(f"Supabase download failed for {object_path}: {e}")

        try:
            target = self._local_path(object_path)
            if target.exists():
                return target.read_bytes()
        except Exception as e:
            logger.error(f"Local download failed for {object_path}: {e}")
        return None

    async def get_signed_url(self, object_path: str, expires_in: int = 3600) -> Optional[str]:
        """Generate signed URL for temporary access."""
        if not settings.is_storage_configured:
            return None
        try:
            result = self.client.storage.from_(self.bucket_name).create_signed_url(
                path=object_path,
                expires_in=expires_in
            )
            return result.get("signedURL") or result.get("signedUrl")
        except Exception as e:
            logger.error(f"Failed to generate signed URL for {object_path}: {e}")
            return None

    async def delete_file(self, object_path: str) -> bool:
        """Delete file from storage."""
        deleted = False
        if settings.is_storage_configured:
            try:
                self.client.storage.from_(self.bucket_name).remove([object_path])
                deleted = True
            except Exception as e:
                logger.warning(f"Supabase delete failed for {object_path}: {e}")

        try:
            target = self._local_path(object_path)
            if target.exists():
                target.unlink()
                deleted = True
        except Exception as e:
            logger.warning(f"Local delete failed for {object_path}: {e}")

        return deleted

    async def delete_patient_files(self, patient_id: UUID) -> bool:
        """Delete all files for a patient."""
        prefix = f"patients/{patient_id}"
        if settings.is_storage_configured:
            try:
                files = self.client.storage.from_(self.bucket_name).list(f"{prefix}/")
                if files:
                    paths = [f"{prefix}/{f['name']}" for f in files if isinstance(f, dict) and 'name' in f]
                    if paths:
                        self.client.storage.from_(self.bucket_name).remove(paths)
            except Exception as e:
                logger.warning(f"Failed to delete patient files from Supabase: {e}")

        try:
            local_dir = self._local_path(prefix)
            if local_dir.exists():
                shutil.rmtree(local_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f"Failed to delete local patient files: {e}")

        return True


# Singleton instance
storage_service = StorageService()
