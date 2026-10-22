"""Document routes for upload, paste, preview, page inspection, and Force OCR."""
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session
from typing import List, Optional
import uuid
import hashlib
from pathlib import Path

from app.database import get_db
from app.models import Patient, Document, DocumentPage, PatientSummary
from app.services.storage import storage_service
from app.services.document_parser import document_parser, DocumentParsingError
from app.config import settings

router = APIRouter(prefix="/documents", tags=["documents"])

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".txt"}


def _detect_and_validate_mime(filename: Optional[str], content: bytes, declared_type: Optional[str]) -> str:
    """
    Validate file extension and magic bytes to determine canonical MIME type.
    Raises ValueError if file format is unsupported or spoofed.
    """
    if not content:
        raise ValueError("File is empty (0 bytes)")

    ext = Path(filename or "").suffix.lower()
    if ext and ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported file extension '{ext}'. Allowed: PDF, PNG, JPG, JPEG, TXT")

    # Magic-byte detection
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"

    # If extension or declared_type indicates PDF or image but magic bytes didn't match, reject
    if ext in {".pdf", ".png", ".jpg", ".jpeg"} or (
        declared_type in {"application/pdf", "image/png", "image/jpeg"}
    ):
        raise ValueError(f"File '{filename}' header does not match a valid {ext or declared_type} file")

    # Plain text validation: ensure no null bytes (binary file masquerading as text)
    if b"\x00" in content[:4096]:
        raise ValueError(f"File '{filename}' appears to be binary, not plain text")

    return "text/plain"


def _serialize_document(doc: Document, pages: List[DocumentPage]) -> dict:
    """Serialize document and readability summary for UI/API."""
    readable_pages = [p.page_number for p in pages if p.is_readable]
    unreadable_pages = [p.page_number for p in pages if not p.is_readable]
    warnings = []
    for p in pages:
        for w in (p.warnings or []):
            warnings.append(f"Page {p.page_number}: {w}")

    return {
        "id": str(doc.id),
        "patient_id": str(doc.patient_id),
        "original_filename": doc.original_filename or "Pasted Clinical Note",
        "content_type": doc.content_type,
        "file_size_bytes": doc.file_size_bytes,
        "source_type": doc.source_type,
        "upload_status": doc.upload_status,
        "parsing_status": doc.parsing_status,
        "page_count": len(pages),
        "readable_page_count": len(readable_pages),
        "unreadable_pages": unreadable_pages,
        "warnings": warnings,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


@router.get("/patient/{patient_id}")
async def list_patient_documents(
    patient_id: str,
    db: Session = Depends(get_db)
):
    """List all documents and page readability details for a patient."""
    try:
        patient_uuid = uuid.UUID(patient_id)
    except ValueError:
        raise HTTPException(400, "Invalid patient ID format")

    docs = (
        db.query(Document)
        .filter(Document.patient_id == patient_uuid)
        .order_by(Document.created_at.asc())
        .all()
    )

    items = []
    for doc in docs:
        pages = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == doc.id)
            .order_by(DocumentPage.page_number.asc())
            .all()
        )
        items.append(_serialize_document(doc, pages))

    return JSONResponse({"documents": items})


@router.get("/{document_id}/pages")
async def get_document_pages(
    document_id: str,
    db: Session = Depends(get_db)
):
    """Get extracted text, blocks, and OCR metadata for all pages of a document."""
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(400, "Invalid document ID format")

    doc = db.query(Document).filter(Document.id == doc_uuid).first()
    if not doc:
        raise HTTPException(404, "Document not found")

    pages = (
        db.query(DocumentPage)
        .filter(DocumentPage.document_id == doc_uuid)
        .order_by(DocumentPage.page_number.asc())
        .all()
    )

    return JSONResponse({
        "document": _serialize_document(doc, pages),
        "pages": [
            {
                "id": str(p.id),
                "page_number": p.page_number,
                "raw_text": p.raw_text or "",
                "normalized_text": p.normalized_text or "",
                "extraction_method": p.extraction_method,
                "ocr_confidence": p.ocr_confidence,
                "is_readable": p.is_readable,
                "warnings": p.warnings or [],
                "blocks": p.blocks or []
            }
            for p in pages
        ]
    })


@router.get("/{document_id}/download")
async def download_document(
    document_id: str,
    db: Session = Depends(get_db)
):
    """Stream original document from private storage for authorized preview/download."""
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(400, "Invalid document ID format")

    doc = db.query(Document).filter(Document.id == doc_uuid).first()
    if not doc:
        raise HTTPException(404, "Document not found")

    content = await storage_service.download_file(doc.storage_path)
    if content is None:
        # Fallback to reconstructing text from pages if pasted note was stored prior to storage integration
        pages = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == doc_uuid)
            .order_by(DocumentPage.page_number.asc())
            .all()
        )
        if pages and doc.content_type == "text/plain":
            content = "\n\n".join(p.raw_text or "" for p in pages).encode("utf-8")
        else:
            raise HTTPException(404, "Original document file not found in storage")

    filename = doc.original_filename or f"document_{doc.id}.txt"
    safe_filename = Path(filename).name.replace('"', "")
    return Response(
        content=content,
        media_type=doc.content_type,
        headers={
            "Content-Disposition": f'inline; filename="{safe_filename}"'
        }
    )


@router.post("/{document_id}/reparse")
async def reparse_document_with_force_ocr(
    document_id: str,
    force_ocr: bool = True,
    db: Session = Depends(get_db)
):
    """Re-parse an existing document with Force OCR enabled."""
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(400, "Invalid document ID format")

    doc = db.query(Document).filter(Document.id == doc_uuid).first()
    if not doc:
        raise HTTPException(404, "Document not found")

    content = await storage_service.download_file(doc.storage_path)
    if content is None:
        raise HTTPException(404, "Original document content unavailable in storage for re-parsing")

    try:
        pages = await document_parser.parse_document(
            content=content,
            content_type=doc.content_type,
            force_ocr=force_ocr
        )

        db.query(DocumentPage).filter(DocumentPage.document_id == doc.id).delete()
        db.flush()

        new_pages = []
        for page_result in pages:
            page = DocumentPage(
                id=uuid.uuid4(),
                document_id=doc.id,
                page_number=page_result.page_number,
                raw_text=page_result.raw_text,
                normalized_text=page_result.normalized_text,
                extraction_method=page_result.extraction_method,
                ocr_confidence=page_result.ocr_confidence,
                blocks=page_result.blocks,
                is_readable=page_result.is_readable,
                warnings=page_result.warnings
            )
            db.add(page)
            new_pages.append(page)

        any_readable = any(p.is_readable for p in new_pages)
        all_readable = all(p.is_readable for p in new_pages)
        doc.parsing_status = "completed" if any_readable else "failed"
        db.commit()

        return JSONResponse({
            "success": True,
            "document": _serialize_document(doc, new_pages),
            "all_readable": all_readable
        })

    except DocumentParsingError as e:
        doc.parsing_status = "failed"
        db.commit()
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=400)


@router.delete("/{document_id}")
async def delete_document(
    document_id: str,
    db: Session = Depends(get_db)
):
    """Delete a document and its stored file."""
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(400, "Invalid document ID format")

    doc = db.query(Document).filter(Document.id == doc_uuid).first()
    if not doc:
        raise HTTPException(404, "Document not found")

    patient_id = doc.patient_id
    storage_path = doc.storage_path
    db.delete(doc)

    # Mark summaries stale
    db.query(PatientSummary).filter(
        PatientSummary.patient_id == patient_id
    ).update({"is_stale": True})
    db.commit()

    if storage_path:
        await storage_service.delete_file(storage_path)

    return JSONResponse({"success": True, "document_id": document_id})


@router.post("/upload/{patient_id}")
async def upload_documents(
    patient_id: str,
    files: List[UploadFile] = File(...),
    force_ocr: bool = Form(False),
    db: Session = Depends(get_db)
):
    """Upload one or more documents for a patient."""
    try:
        patient_uuid = uuid.UUID(patient_id)
    except ValueError:
        raise HTTPException(400, "Invalid patient ID format")

    patient = db.query(Patient).filter(Patient.id == patient_uuid).first()
    if not patient:
        raise HTTPException(404, "Patient not found")

    if len(files) > settings.max_files_per_upload:
        raise HTTPException(400, f"Maximum {settings.max_files_per_upload} files allowed per upload")

    results = []
    accepted_batch_bytes = 0

    for file in files:
        content = await file.read()
        file_size = len(content)

        if file_size > settings.max_file_bytes:
            results.append({
                "filename": file.filename,
                "success": False,
                "error": f"File exceeds {settings.max_file_mb}MB limit"
            })
            continue

        if accepted_batch_bytes + file_size > settings.max_batch_bytes:
            results.append({
                "filename": file.filename,
                "success": False,
                "error": f"Batch exceeds {settings.max_batch_mb}MB total limit"
            })
            continue

        try:
            validated_mime = _detect_and_validate_mime(file.filename, content, file.content_type)
        except ValueError as ve:
            results.append({
                "filename": file.filename,
                "success": False,
                "error": str(ve)
            })
            continue

        try:
            content_hash = hashlib.sha256(content).hexdigest()

            existing = db.query(Document).filter(
                Document.patient_id == patient_uuid,
                Document.content_hash == content_hash
            ).first()

            if existing:
                if existing.parsing_status == "failed":
                    db.delete(existing)
                    db.flush()
                else:
                    results.append({
                        "filename": file.filename,
                        "success": False,
                        "error": "Duplicate document already uploaded for this patient",
                        "document_id": str(existing.id)
                    })
                    continue

            doc_id = uuid.uuid4()
            ext = Path(file.filename or "").suffix.lower() or ".bin"
            document = Document(
                id=doc_id,
                patient_id=patient_uuid,
                original_filename=Path(file.filename or f"upload{ext}").name,
                content_type=validated_mime,
                file_size_bytes=file_size,
                content_hash=content_hash,
                storage_bucket=settings.supabase_storage_bucket,
                storage_path="",
                source_type="upload",
                upload_status="pending",
                parsing_status="pending"
            )

            storage_result = await storage_service.upload_file(
                patient_uuid,
                doc_id,
                f"original{ext}",
                content,
                validated_mime
            )

            if not storage_result["success"]:
                results.append({
                    "filename": file.filename,
                    "success": False,
                    "error": f"Storage upload failed: {storage_result.get('error')}"
                })
                continue

            accepted_batch_bytes += file_size
            document.storage_path = storage_result["object_path"]
            document.upload_status = "completed"
            db.add(document)
            db.flush()

            try:
                pages = await document_parser.parse_document(
                    content,
                    validated_mime,
                    force_ocr=force_ocr
                )

                unreadable_pages = []
                for page_result in pages:
                    if not page_result.is_readable:
                        unreadable_pages.append(page_result.page_number)
                    page = DocumentPage(
                        id=uuid.uuid4(),
                        document_id=doc_id,
                        page_number=page_result.page_number,
                        raw_text=page_result.raw_text,
                        normalized_text=page_result.normalized_text,
                        extraction_method=page_result.extraction_method,
                        ocr_confidence=page_result.ocr_confidence,
                        blocks=page_result.blocks,
                        is_readable=page_result.is_readable,
                        warnings=page_result.warnings
                    )
                    db.add(page)

                document.parsing_status = "completed"

                # Mark any existing patient summaries stale when new documents are added
                db.query(PatientSummary).filter(
                    PatientSummary.patient_id == patient_uuid
                ).update({"is_stale": True})

                db.commit()

                results.append({
                    "filename": file.filename,
                    "success": True,
                    "document_id": str(doc_id),
                    "pages": len(pages),
                    "unreadable_pages": unreadable_pages
                })

            except DocumentParsingError as e:
                document.parsing_status = "failed"
                db.commit()
                results.append({
                    "filename": file.filename,
                    "success": False,
                    "error": f"Parsing failed: {str(e)}",
                    "document_id": str(doc_id)
                })

        except Exception as e:
            db.rollback()
            results.append({
                "filename": file.filename,
                "success": False,
                "error": str(e)
            })

    return JSONResponse({"results": results})


@router.post("/paste/{patient_id}")
async def paste_text(
    patient_id: str,
    text: str = Form(...),
    title: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    """Paste clinical note text as a document."""
    try:
        try:
            patient_uuid = uuid.UUID(patient_id)
        except ValueError:
            return JSONResponse({
                "success": False,
                "error": "Invalid patient ID format"
            }, status_code=400)

        patient = db.query(Patient).filter(Patient.id == patient_uuid).first()
        if not patient:
            return JSONResponse({
                "success": False,
                "error": "Patient not found"
            }, status_code=404)

        cleaned_text = (text or "").strip()
        if not cleaned_text:
            return JSONResponse({
                "success": False,
                "error": "Pasted text cannot be empty"
            }, status_code=400)

        content = cleaned_text.encode("utf-8")
        if len(content) > settings.max_file_bytes:
            return JSONResponse({
                "success": False,
                "error": f"Text exceeds {settings.max_file_mb}MB limit"
            }, status_code=400)

        content_hash = hashlib.sha256(content).hexdigest()

        existing = db.query(Document).filter(
            Document.patient_id == patient_uuid,
            Document.content_hash == content_hash
        ).first()

        if existing:
            return JSONResponse({
                "success": False,
                "error": "Duplicate text note already exists for this patient",
                "document_id": str(existing.id)
            })

        doc_id = uuid.uuid4()
        note_filename = (title or "").strip() or f"pasted_note_{str(doc_id)[:8]}.txt"
        if not note_filename.lower().endswith(".txt"):
            note_filename += ".txt"

        storage_result = await storage_service.upload_file(
            patient_uuid,
            doc_id,
            note_filename,
            content,
            "text/plain"
        )
        storage_path = storage_result.get("object_path") or f"local/paste/{doc_id}.txt"

        document = Document(
            id=doc_id,
            patient_id=patient_uuid,
            original_filename=note_filename,
            content_type="text/plain",
            file_size_bytes=len(content),
            content_hash=content_hash,
            storage_bucket=storage_result.get("bucket", settings.supabase_storage_bucket),
            storage_path=storage_path,
            source_type="paste",
            upload_status="completed",
            parsing_status="pending"
        )

        db.add(document)
        db.flush()

        try:
            pages = await document_parser.parse_document(content, "text/plain")

            for page_result in pages:
                page = DocumentPage(
                    id=uuid.uuid4(),
                    document_id=doc_id,
                    page_number=page_result.page_number,
                    raw_text=page_result.raw_text,
                    normalized_text=page_result.normalized_text,
                    extraction_method="paste",
                    blocks=page_result.blocks,
                    is_readable=page_result.is_readable,
                    warnings=page_result.warnings
                )
                db.add(page)

            document.parsing_status = "completed"

            db.query(PatientSummary).filter(
                PatientSummary.patient_id == patient_uuid
            ).update({"is_stale": True})

            db.commit()

            return JSONResponse({
                "success": True,
                "document_id": str(doc_id),
                "filename": note_filename
            })

        except Exception as e:
            document.parsing_status = "failed"
            db.commit()
            return JSONResponse({
                "success": False,
                "error": f"Parsing failed: {str(e)}",
                "document_id": str(doc_id)
            }, status_code=500)

    except Exception as e:
        db.rollback()
        return JSONResponse({
            "success": False,
            "error": f"Server error: {str(e)}"
        }, status_code=500)
