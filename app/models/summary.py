"""Patient summary model."""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, JSON, Boolean, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class PatientSummary(Base):
    """Generated patient description."""

    __tablename__ = "patient_summaries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)

    # Summary content
    summary_text = Column(Text, nullable=False)
    sentences = Column(JSON, nullable=False)
    # Array of: {text, fact_ids: []}

    # Status
    status = Column(String, default="generated", nullable=False)
    # generated, failed
    is_stale = Column(Boolean, default=False, nullable=False)

    # Validation
    validation_errors = Column(JSON, nullable=True)

    # Source snapshot
    fact_revision = Column(Integer, nullable=False)  # Count of facts at generation time
    snapshot_id = Column(UUID(as_uuid=True), nullable=True)

    # Timestamps
    generated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    patient = relationship("Patient", back_populates="summaries")
