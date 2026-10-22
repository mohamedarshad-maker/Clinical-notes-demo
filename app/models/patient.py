"""Patient model."""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class Patient(Base):
    """Patient record for demo purposes."""

    __tablename__ = "patients"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    display_name = Column(String, nullable=False)
    is_demo = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    documents = relationship("Document", back_populates="patient", cascade="all, delete-orphan")
    processing_jobs = relationship("ProcessingJob", back_populates="patient", cascade="all, delete-orphan")
    facts = relationship("ConsolidatedFact", back_populates="patient", cascade="all, delete-orphan")
    summaries = relationship("PatientSummary", back_populates="patient", cascade="all, delete-orphan")
