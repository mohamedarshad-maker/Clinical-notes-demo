"""Pydantic schemas for validation."""
from app.schemas.clinical import (
    CATEGORY_GROUPS,
    ClinicalEvidence,
    ExtractionGroupOutput,
    CombinedExtractionOutput
)
from app.schemas.patient import PatientCreate, PatientResponse
from app.schemas.document import DocumentResponse, DocumentPageResponse
from app.schemas.fact import FactResponse, FactUpdate
from app.schemas.summary import SummaryResponse

__all__ = [
    "CATEGORY_GROUPS",
    "ClinicalEvidence",
    "ExtractionGroupOutput",
    "CombinedExtractionOutput",
    "PatientCreate",
    "PatientResponse",
    "DocumentResponse",
    "DocumentPageResponse",
    "FactResponse",
    "FactUpdate",
    "SummaryResponse",
]
