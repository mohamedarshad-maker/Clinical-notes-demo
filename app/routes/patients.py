"""Patient routes."""
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
import uuid

from app.config import settings
from app.database import get_db
from app.models import Patient
from app.services.storage import storage_service

router = APIRouter(prefix="/patients", tags=["patients"])
templates = Jinja2Templates(directory="app/templates")


def _get_readiness() -> dict:
    return {
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "llm_configured": settings.is_llm_configured,
        "who_icd_configured": settings.is_who_icd_configured,
        "storage_configured": settings.is_any_storage_configured,
        "ocr_configured": settings.is_ocr_configured,
        "tesseract_configured": settings.is_tesseract_configured
    }


@router.get("/", response_class=HTMLResponse)
async def list_patients(request: Request, db: Session = Depends(get_db)):
    """List all patients."""
    patients = (
        db.query(Patient)
        .options(joinedload(Patient.documents))
        .order_by(Patient.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        request=request,
        name="patients/list.html",
        context={
            "request": request,
            "patients": patients,
            "readiness": _get_readiness()
        }
    )


@router.post("/", response_class=HTMLResponse)
async def create_patient(
    display_name: str = Form(...),
    db: Session = Depends(get_db)
):
    """Create new demo patient."""
    cleaned_name = (display_name or "").strip()
    if not cleaned_name:
        raise HTTPException(400, "Patient display name cannot be empty")

    patient = Patient(
        id=uuid.uuid4(),
        display_name=cleaned_name,
        is_demo=True
    )
    db.add(patient)
    db.commit()
    db.refresh(patient)

    return RedirectResponse(url=f"/patients/{patient.id}", status_code=303)


@router.get("/{patient_id}", response_class=HTMLResponse)
async def get_patient(
    patient_id: str,
    request: Request,
    db: Session = Depends(get_db)
):
    """Get patient detail workspace."""
    try:
        patient_uuid = uuid.UUID(patient_id)
    except ValueError:
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"request": request, "message": "Invalid patient ID format"},
            status_code=400
        )

    patient = db.query(Patient).filter(Patient.id == patient_uuid).first()
    if not patient:
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"request": request, "message": "Patient not found"},
            status_code=404
        )

    return templates.TemplateResponse(
        request=request,
        name="patients/detail.html",
        context={
            "request": request,
            "patient": patient,
            "readiness": _get_readiness()
        }
    )


@router.delete("/{patient_id}")
async def delete_patient(
    patient_id: str,
    db: Session = Depends(get_db)
):
    """Delete a demo patient and all associated storage files."""
    try:
        patient_uuid = uuid.UUID(patient_id)
    except ValueError:
        raise HTTPException(400, "Invalid patient ID format")

    patient = db.query(Patient).filter(Patient.id == patient_uuid).first()
    if not patient:
        raise HTTPException(404, "Patient not found")

    db.delete(patient)
    db.commit()

    await storage_service.delete_patient_files(patient_uuid)
    return JSONResponse({"success": True, "patient_id": patient_id})
