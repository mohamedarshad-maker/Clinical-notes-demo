"""ICD-11 mapping models."""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, JSON, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class ICDCandidate(Base):
    """WHO ICD-11 MMS candidate from lookup."""

    __tablename__ = "icd_candidates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Search context
    diagnosis_text = Column(String, nullable=False)
    normalized_query = Column(String, nullable=False)  # For cache lookup

    # WHO ICD-11 details
    mms_code = Column(String, nullable=False)
    mms_title = Column(String, nullable=False)
    mms_description = Column(String, nullable=True)
    foundation_uri = Column(String, nullable=True)
    who_source_link = Column(String, nullable=True)

    # Configuration
    icd_release = Column(String, nullable=False)
    icd_language = Column(String, nullable=False)

    # Search metadata
    search_rank = Column(Integer, nullable=False)  # Position in WHO search results
    lookup_timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    decisions = relationship("ICDDecision", back_populates="candidate")


class ICDDecision(Base):
    """Reviewer decision on ICD mapping for a diagnosis fact."""

    __tablename__ = "icd_decisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fact_id = Column(UUID(as_uuid=True), ForeignKey("consolidated_facts.id", ondelete="CASCADE"), nullable=False)
    candidate_id = Column(UUID(as_uuid=True), ForeignKey("icd_candidates.id", ondelete="SET NULL"), nullable=True)

    # Decision
    mapping_status = Column(String, default="pending", nullable=False)
    # pending, suggested, accepted, rejected, unresolved, lookup_failed
    needs_review = Column(Boolean, default=False, nullable=False)  # Set when diagnosis edited

    # Context
    refined_query = Column(String, nullable=True)  # If reviewer refined the search
    reviewer_note = Column(String, nullable=True)

    # Configuration snapshot
    icd_release = Column(String, nullable=False)
    icd_language = Column(String, nullable=False)

    # Timestamps
    suggested_at = Column(DateTime, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    invalidated_at = Column(DateTime, nullable=True)  # When marked needs_review

    # Relationships
    fact = relationship("ConsolidatedFact", back_populates="icd_decisions")
    candidate = relationship("ICDCandidate", back_populates="decisions")
