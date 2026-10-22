"""Export routes."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
import io

from app.database import get_db
from app.services.export import create_export_service

router = APIRouter(prefix="/export", tags=["export"])


@router.get("/facts/{patient_id}")
async def export_facts(
    patient_id: str,
    include_unaccepted_icd: bool = False,
    include_not_documented: bool = True,
    include_rejected: bool = False,
    db: Session = Depends(get_db)
):
    """Export patient clinical facts to CSV."""
    export_service = create_export_service(db)

    try:
        csv_content = export_service.export_clinical_facts(
            patient_id=patient_id,
            include_unaccepted_icd=include_unaccepted_icd,
            include_not_documented=include_not_documented,
            include_rejected=include_rejected
        )

        return StreamingResponse(
            io.StringIO(csv_content),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f"attachment; filename=patient_{patient_id}_clinical_facts.csv"
            }
        )

    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/summary/{patient_id}")
async def export_summary(
    patient_id: str,
    db: Session = Depends(get_db)
):
    """Export patient summary to CSV."""
    export_service = create_export_service(db)

    try:
        csv_content = export_service.export_patient_summary(patient_id)

        return StreamingResponse(
            io.StringIO(csv_content),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f"attachment; filename=patient_{patient_id}_summary.csv"
            }
        )

    except ValueError as e:
        raise HTTPException(404, str(e))
