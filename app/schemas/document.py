"""Document schemas."""
from pydantic import BaseModel, ConfigDict
from datetime import datetime
from uuid import UUID
from typing import List, Optional, Any


class DocumentResponse(BaseModel):
    """Document response."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    patient_id: UUID
    original_filename: Optional[str] = None
    content_type: str
    file_size_bytes: int
    source_type: str
    upload_status: str
    parsing_status: str
    parsing_error: Optional[str] = None
    created_at: datetime


class DocumentPageResponse(BaseModel):
    """Document page response."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    page_number: int
    raw_text: Optional[str] = None
    blocks: Optional[List[Any]] = None
    extraction_method: str
    ocr_confidence: Optional[int] = None
    is_readable: bool
    warnings: Optional[List[str]] = None
