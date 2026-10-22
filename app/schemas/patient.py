"""Patient schemas."""
from pydantic import BaseModel, ConfigDict
from datetime import datetime
from uuid import UUID


class PatientCreate(BaseModel):
    """Create a new patient."""
    model_config = ConfigDict(extra="forbid")

    display_name: str
    is_demo: bool = True


class PatientResponse(BaseModel):
    """Patient response."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    display_name: str
    is_demo: bool
    created_at: datetime
    updated_at: datetime
