"""Fact schemas."""
from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime
from uuid import UUID
from typing import List, Optional, Any


class FactResponse(BaseModel):
    """Clinical fact response."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    patient_id: UUID
    category_number: int
    category_key: str
    entry_id: str
    field_name: str
    value: Optional[str] = None
    value_json: Optional[Any] = None
    availability: str
    assertion: Optional[str] = None
    temporality: Optional[str] = None
    experiencer: Optional[str] = None
    event_time: Optional[str] = None
    original_time_expression: Optional[str] = None
    evidence: List[dict] = Field(default_factory=list)
    origin: str
    review_status: str
    extracted_at: datetime
    reviewed_at: Optional[datetime] = None


class FactUpdate(BaseModel):
    """Update a clinical fact."""
    model_config = ConfigDict(extra="forbid")

    value: Optional[str] = None
    unit: Optional[str] = None
    availability: Optional[str] = None
    assertion: Optional[str] = None
    temporality: Optional[str] = None
    experiencer: Optional[str] = None
    event_time: Optional[str] = None
    review_status: Optional[str] = None
    reviewer_note: Optional[str] = None


class FactCreate(BaseModel):
    """Add a new clinical fact manually."""
    model_config = ConfigDict(extra="forbid")

    category_number: int
    field_name: str
    value: str
    unit: Optional[str] = None
    entry_id: Optional[str] = None
    assertion: Optional[str] = "present"
    temporality: Optional[str] = "current"
    experiencer: Optional[str] = "patient"
    event_time: Optional[str] = None
    document_id: Optional[str] = None
    page_number: Optional[int] = 1
    block_id: Optional[str] = None
    quote: Optional[str] = None
    reviewer_note: Optional[str] = None
    manual_icd_code: Optional[str] = None
    manual_icd_title: Optional[str] = None
    auto_icd_lookup: Optional[bool] = True
