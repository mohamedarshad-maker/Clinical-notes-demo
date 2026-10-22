"""Processing job and extraction models."""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, JSON, Boolean, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class ProcessingJob(Base):
    """Processing job for a patient's document set."""

    __tablename__ = "processing_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)

    # Job status
    status = Column(String, default="pending", nullable=False)
    # States: pending, parsing, extracting, summarizing, icd_lookup, completed, failed, interrupted

    # Stage tracking
    stages = Column(JSON, nullable=False, default=dict)
    # Format: {"parsing": "completed", "extraction_a": "completed", "extraction_b": "failed", ...}

    # Progress
    current_stage = Column(String, nullable=True)
    error_message = Column(String, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    patient = relationship("Patient", back_populates="processing_jobs")
    snapshot = relationship("ExtractionSnapshot", back_populates="job", uselist=False)
    extraction_runs = relationship("ExtractionRun", back_populates="job", cascade="all, delete-orphan")


class ExtractionSnapshot(Base):
    """Immutable snapshot of documents for extraction."""

    __tablename__ = "extraction_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id = Column(UUID(as_uuid=True), ForeignKey("processing_jobs.id", ondelete="CASCADE"), nullable=False, unique=True)

    # Snapshot content
    document_ids = Column(JSON, nullable=False)  # Array of document UUIDs
    document_hashes = Column(JSON, nullable=False)  # Map of document_id -> content_hash
    patient_revision = Column(Integer, nullable=False)

    # Configuration
    schema_version = Column(String, nullable=False)
    prompt_version = Column(String, nullable=False)
    llm_provider = Column(String, nullable=False)
    llm_model = Column(String, nullable=False)

    # Full text for extraction (denormalized for immutability)
    combined_text = Column(Text, nullable=False)
    document_structure = Column(JSON, nullable=False)  # Document/page/block boundaries

    # Token estimation
    estimated_input_tokens = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    job = relationship("ProcessingJob", back_populates="snapshot")


class ExtractionRun(Base):
    """Individual extraction run for a category group."""

    __tablename__ = "extraction_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id = Column(UUID(as_uuid=True), ForeignKey("processing_jobs.id", ondelete="CASCADE"), nullable=False)

    # Group identification
    group_name = Column(String, nullable=False)  # 'A', 'B', or 'C'
    category_numbers = Column(JSON, nullable=False)  # Array of category numbers [1-19]

    # Status
    status = Column(String, default="pending", nullable=False)  # pending, running, completed, failed
    retry_count = Column(Integer, default=0, nullable=False)

    # Results
    raw_response = Column(JSON, nullable=True)
    validated_output = Column(JSON, nullable=True)
    validation_errors = Column(JSON, nullable=True)

    # Execution details
    llm_call_duration_ms = Column(Integer, nullable=True)
    error_message = Column(String, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # Relationships
    job = relationship("ProcessingJob", back_populates="extraction_runs")
