"""Processing routes for analysis, retry, summary regeneration, and status."""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional
import uuid
import logging
import traceback

from app.database import get_db, SessionLocal
from app.models import ProcessingJob, Document, DocumentPage, ExtractionRun
from app.services.processing import (
    create_processing_service,
    get_active_job,
    recover_stale_jobs
)

router = APIRouter(prefix="/processing", tags=["processing"])
logger = logging.getLogger(__name__)


class AnalyzeRequest(BaseModel):
    """Optional parameters for triggering patient analysis."""
    document_ids: Optional[List[str]] = None
    acknowledge_unreadable: bool = False


@router.post("/analyze/{patient_id}")
async def analyze_patient(
    patient_id: str,
    background_tasks: BackgroundTasks,
    payload: Optional[AnalyzeRequest] = Body(default=None),
    db: Session = Depends(get_db)
):
    """Start analysis of patient documents with admission and readability checks."""
    try:
        try:
            patient_uuid = uuid.UUID(patient_id)
        except ValueError:
            return JSONResponse({
                "success": False,
                "error": "Invalid patient ID format"
            }, status_code=400)

        # Single active analysis admission control
        active_job = get_active_job(db)
        if active_job:
            return JSONResponse({
                "success": False,
                "error": "Another analysis job is currently running. Please wait for it to finish.",
                "active_job_id": str(active_job.id)
            }, status_code=409)

        # Resolve target documents
        query = db.query(Document).filter(
            Document.patient_id == patient_uuid,
            Document.parsing_status == "completed"
        )
        if payload and payload.document_ids:
            try:
                target_uuids = [uuid.UUID(did) for did in payload.document_ids]
            except ValueError:
                return JSONResponse({
                    "success": False,
                    "error": "Invalid document ID in document_ids"
                }, status_code=400)
            query = query.filter(Document.id.in_(target_uuids))

        documents = query.order_by(Document.created_at.asc()).all()

        if not documents:
            return JSONResponse({
                "success": False,
                "error": "No parsed documents available. Please upload or paste clinical notes first."
            }, status_code=400)

        # Check page readability before queuing LLM extraction
        readable_count = 0
        unreadable_details = []
        for doc in documents:
            pages = (
                db.query(DocumentPage)
                .filter(DocumentPage.document_id == doc.id)
                .order_by(DocumentPage.page_number.asc())
                .all()
            )
            for p in pages:
                if p.is_readable:
                    readable_count += 1
                else:
                    unreadable_details.append({
                        "document_id": str(doc.id),
                        "filename": doc.original_filename or "Pasted Note",
                        "page_number": p.page_number,
                        "warnings": p.warnings or []
                    })

        if readable_count == 0:
            return JSONResponse({
                "success": False,
                "error": "Every page in the selected document set is unreadable. Please re-upload clearer files or run Force OCR.",
                "unreadable_pages": unreadable_details
            }, status_code=400)

        allow_partial = bool(payload and payload.acknowledge_unreadable)
        if unreadable_details and not allow_partial:
            return JSONResponse({
                "success": False,
                "requires_acknowledgment": True,
                "error": (
                    f"{len(unreadable_details)} page(s) are unreadable. "
                    "Re-run with Force OCR, replace the document, or confirm continuing with readable pages only."
                ),
                "unreadable_pages": unreadable_details
            }, status_code=409)

        document_ids = [doc.id for doc in documents]

        # Create the job row synchronously so status polling sees it immediately
        job = ProcessingJob(
            id=uuid.uuid4(),
            patient_id=patient_uuid,
            status="pending",
            stages={"parsing": "completed"}
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

        async def _run_background_analysis():
            bg_db = SessionLocal()
            try:
                service = create_processing_service(bg_db)
                await service.process_patient_documents(
                    patient_id=patient_uuid,
                    document_ids=document_ids,
                    allow_partial_readable=allow_partial,
                    existing_job_id=job_id
                )
                logger.info(f"Background processing completed for patient {patient_id} (job {job_id})")
            except Exception as e:
                logger.error(f"Background processing error for patient {patient_id}: {e}")
                logger.error(traceback.format_exc())
            finally:
                bg_db.close()

        background_tasks.add_task(_run_background_analysis)

        return JSONResponse({
            "success": True,
            "message": "Processing started",
            "job_id": str(job_id),
            "patient_id": patient_id,
            "document_count": len(documents)
        })

    except Exception as e:
        logger.error(f"Analyze endpoint error: {e}")
        logger.error(traceback.format_exc())
        return JSONResponse({
            "success": False,
            "error": f"Server error: {str(e)}"
        }, status_code=500)


@router.post("/retry/{patient_id}")
async def retry_patient_processing(
    patient_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """Retry failed or interrupted stages for the latest processing job of a patient."""
    try:
        patient_uuid = uuid.UUID(patient_id)
    except ValueError:
        return JSONResponse({"success": False, "error": "Invalid patient ID format"}, status_code=400)

    active_job = get_active_job(db)
    if active_job:
        return JSONResponse({
            "success": False,
            "error": "Another analysis job is currently running."
        }, status_code=409)

    job = (
        db.query(ProcessingJob)
        .filter(ProcessingJob.patient_id == patient_uuid)
        .order_by(ProcessingJob.created_at.desc())
        .first()
    )
    if not job:
        return JSONResponse({
            "success": False,
            "error": "No previous processing job found for this patient."
        }, status_code=404)

    job.status = "pending"
    job.error_message = None
    db.commit()
    job_id = job.id

    async def _run_background_retry():
        bg_db = SessionLocal()
        try:
            service = create_processing_service(bg_db)
            await service.retry_job(job_id=job_id, allow_partial_readable=True)
        except Exception as e:
            logger.error(f"Background retry failed for job {job_id}: {e}")
        finally:
            bg_db.close()

    background_tasks.add_task(_run_background_retry)

    return JSONResponse({
        "success": True,
        "message": "Retry started for failed/interrupted stages",
        "job_id": str(job_id)
    })


@router.post("/summary/{patient_id}")
async def regenerate_summary(
    patient_id: str,
    db: Session = Depends(get_db)
):
    """Regenerate grounded patient summary from current consolidated facts without rerunning extraction."""
    try:
        patient_uuid = uuid.UUID(patient_id)
    except ValueError:
        return JSONResponse({"success": False, "error": "Invalid patient ID format"}, status_code=400)

    try:
        service = create_processing_service(db)
        summary = await service.regenerate_patient_summary(patient_uuid)
        return JSONResponse({
            "success": True,
            "summary": {
                "id": str(summary.id),
                "summary_text": summary.summary_text,
                "sentences": summary.sentences,
                "status": summary.status,
                "is_stale": summary.is_stale,
                "validation_errors": summary.validation_errors,
                "generated_at": summary.generated_at.isoformat() if summary.generated_at else None
            }
        })
    except Exception as e:
        logger.error(f"Summary regeneration error: {e}")
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=500)


@router.get("/status/{patient_id}")
async def get_processing_status(
    patient_id: str,
    db: Session = Depends(get_db)
):
    """Get processing status and per-group extraction details for a patient."""
    try:
        patient_uuid = uuid.UUID(patient_id)
    except ValueError:
        raise HTTPException(400, "Invalid patient ID format")

    recover_stale_jobs(db, force_all_active=False)

    job = (
        db.query(ProcessingJob)
        .filter(ProcessingJob.patient_id == patient_uuid)
        .order_by(ProcessingJob.created_at.desc())
        .first()
    )

    if not job:
        return JSONResponse({
            "status": "not_started",
            "stages": {},
            "runs": []
        })

    runs = (
        db.query(ExtractionRun)
        .filter(ExtractionRun.job_id == job.id)
        .order_by(ExtractionRun.group_name.asc())
        .all()
    )

    return JSONResponse({
        "job_id": str(job.id),
        "status": job.status,
        "stages": job.stages or {},
        "current_stage": job.current_stage,
        "error_message": job.error_message,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "runs": [
            {
                "group_name": r.group_name,
                "category_numbers": r.category_numbers,
                "status": r.status,
                "retry_count": r.retry_count,
                "duration_ms": r.llm_call_duration_ms,
                "error_message": r.error_message
            }
            for r in runs
        ]
    })
