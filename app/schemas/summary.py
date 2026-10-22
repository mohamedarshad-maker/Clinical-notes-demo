"""Summary schemas."""
from pydantic import BaseModel, ConfigDict
from datetime import datetime
from uuid import UUID
from typing import List, Optional


class SummaryResponse(BaseModel):
    """Patient summary response."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    patient_id: UUID
    summary_text: str
    sentences: List[dict]
    status: str
    is_stale: bool
    validation_errors: Optional[List[str]] = None
    generated_at: datetime
