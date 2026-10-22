"""Document and page models."""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Text, JSON, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class Document(Base):
    """Uploaded or pasted clinical document."""

    __tablename__ = "documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)

    # File metadata
    original_filename = Column(String, nullable=True)  # Null for pasted text
    content_type = Column(String, nullable=False)
    file_size_bytes = Column(Integer, nullable=False)
    content_hash = Column(String, nullable=False)  # For deduplication

    # Storage
    storage_bucket = Column(String, nullable=False)
    storage_path = Column(String, nullable=False)

    # Document type
    source_type = Column(String, nullable=False)  # 'upload' or 'paste'

    # Status
    upload_status = Column(String, default="pending", nullable=False)  # pending, completed, failed
    parsing_status = Column(String, default="pending", nullable=False)  # pending, completed, partial, failed

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    patient = relationship("Patient", back_populates="documents")
    pages = relationship("DocumentPage", back_populates="document", cascade="all, delete-orphan")


class DocumentPage(Base):
    """Individual page from a document with extracted text."""

    __tablename__ = "document_pages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)

    page_number = Column(Integer, nullable=False)

    # Extracted text
    raw_text = Column(Text, nullable=True)
    normalized_text = Column(Text, nullable=True)

    # Extraction method
    extraction_method = Column(String, nullable=False)  # 'direct', 'ocr', 'paste'
    ocr_confidence = Column(Integer, nullable=True)  # 0-100 if available

    # Structured content
    blocks = Column(JSON, nullable=True)  # Array of text blocks with IDs and positions

    # Status
    is_readable = Column(Boolean, default=True, nullable=False)
    warnings = Column(JSON, nullable=True)  # Array of warning messages

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    document = relationship("Document", back_populates="pages")
