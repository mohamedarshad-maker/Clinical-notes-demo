"""Clinical fact and review models."""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, JSON, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class ConsolidatedFact(Base):
    """Consolidated clinical fact from extraction."""

    __tablename__ = "consolidated_facts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)

    # Category and structure
    category_number = Column(Integer, nullable=False)  # 1-19
    category_key = Column(String, nullable=False)  # e.g., 'patient_details', 'medications'
    entry_id = Column(String, nullable=False)  # Stable identifier for grouping related subfields

    # Fact content
    field_name = Column(String, nullable=False)
    value = Column(String, nullable=True)
    value_json = Column(JSON, nullable=True)  # For complex structured values

    # Clinical metadata
    availability = Column(String, default="documented", nullable=False)
    # documented, not_documented, unreadable
    assertion = Column(String, nullable=True)
    # present, negated, uncertain, unknown
    temporality = Column(String, nullable=True)
    # current, historical, planned, resolved, unknown
    experiencer = Column(String, nullable=True)
    # patient, family_member, other, unknown
    event_time = Column(String, nullable=True)
    original_time_expression = Column(String, nullable=True)

    # Evidence
    evidence = Column(JSON, nullable=False)
    # Array of: {document_id, page_number, block_id, quote}

    # Origin and review
    origin = Column(String, default="extraction", nullable=False)
    # extraction, reviewer_correction, reviewer_addition, derived
    review_status = Column(String, default="unreviewed", nullable=False)
    # unreviewed, confirmed, corrected, rejected

    # Timestamps
    extracted_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    reviewed_at = Column(DateTime, nullable=True)

    # Relationships
    patient = relationship("Patient", back_populates="facts")
    revisions = relationship("ReviewRevision", back_populates="fact", cascade="all, delete-orphan")
    icd_decisions = relationship("ICDDecision", back_populates="fact", cascade="all, delete-orphan")


class ReviewRevision(Base):
    """History of reviewer changes to a fact."""

    __tablename__ = "review_revisions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fact_id = Column(UUID(as_uuid=True), ForeignKey("consolidated_facts.id", ondelete="CASCADE"), nullable=False)

    # Change details
    action = Column(String, nullable=False)  # edit, confirm, reject, add
    previous_value = Column(String, nullable=True)
    new_value = Column(String, nullable=True)
    changed_fields = Column(JSON, nullable=True)  # List of field names changed

    # Reviewer info (demo mode - just timestamp)
    reviewer_note = Column(String, nullable=True)
    revised_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    fact = relationship("ConsolidatedFact", back_populates="revisions")
