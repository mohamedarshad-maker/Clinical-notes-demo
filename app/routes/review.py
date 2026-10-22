"""Routes for patient profile data, fact review/editing/addition, and WHO ICD-11 review."""
from datetime import datetime
from typing import Optional
import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload, selectinload

from app.config import settings
from app.database import get_db
from app.models import (
    Patient, Document, DocumentPage, ProcessingJob, ExtractionSnapshot,
    ExtractionRun, ConsolidatedFact, ReviewRevision, PatientSummary,
    ICDCandidate, ICDDecision
)
from app.schemas.clinical import CATEGORY_DEFINITIONS
from app.schemas.fact import FactUpdate, FactCreate
from app.services.who_icd import who_icd_service, WHOICDError

router = APIRouter(tags=["review"])
logger = logging.getLogger(__name__)


class ICDDecisionUpdate(BaseModel):
    """Reviewer decision on an ICD-11 candidate mapping."""
    model_config = ConfigDict(extra="forbid")

    mapping_status: str  # accepted, rejected, unresolved, suggested
    candidate_id: Optional[str] = None
    manual_code: Optional[str] = None
    manual_title: Optional[str] = None
    reviewer_note: Optional[str] = None


class ICDLookupRequest(BaseModel):
    """Request to retry or refine WHO ICD-11 search for a diagnosis fact."""
    model_config = ConfigDict(extra="forbid")

    refined_query: Optional[str] = None


class BatchFactReviewRequest(BaseModel):
    """Batch confirm or reject multiple clinical facts in a single request."""
    model_config = ConfigDict(extra="forbid")

    fact_ids: list[str]
    review_status: str
    reviewer_note: Optional[str] = None


class BatchICDDecisionItem(BaseModel):
    """Single item in a batch ICD decision request."""
    model_config = ConfigDict(extra="forbid")

    fact_id: str
    candidate_id: str
    mapping_status: str = "accepted"
    reviewer_note: Optional[str] = None


class BatchICDDecisionRequest(BaseModel):
    """Batch accept/update multiple ICD-11 decisions in a single request."""
    model_config = ConfigDict(extra="forbid")

    decisions: list[BatchICDDecisionItem]


def _serialize_fact(fact: ConsolidatedFact, doc_name_map: dict) -> dict:
    """Serialize a ConsolidatedFact with unit, evidence, and revision history."""
    unit = None
    snapshot_id = None
    if isinstance(fact.value_json, dict):
        unit = fact.value_json.get("unit")
        snapshot_id = fact.value_json.get("snapshot_id")

    enriched_evidence = []
    for ev in (fact.evidence or []):
        doc_id = str(ev.get("document_id") or "")
        enriched_evidence.append({
            "document_id": doc_id,
            "document_name": doc_name_map.get(doc_id, "Document"),
            "page_number": ev.get("page_number", 1),
            "block_id": ev.get("block_id"),
            "quote": ev.get("quote", "")
        })

    return {
        "id": str(fact.id),
        "patient_id": str(fact.patient_id),
        "category_number": fact.category_number,
        "category_key": fact.category_key,
        "entry_id": fact.entry_id,
        "field_name": fact.field_name,
        "value": fact.value,
        "unit": unit,
        "snapshot_id": snapshot_id,
        "availability": fact.availability,
        "assertion": fact.assertion,
        "temporality": fact.temporality,
        "experiencer": fact.experiencer,
        "event_time": fact.event_time,
        "original_time_expression": fact.original_time_expression,
        "evidence": enriched_evidence,
        "origin": fact.origin,
        "review_status": fact.review_status,
        "extracted_at": fact.extracted_at.isoformat() if fact.extracted_at else None,
        "reviewed_at": fact.reviewed_at.isoformat() if fact.reviewed_at else None,
        "revisions": [
            {
                "id": str(rev.id),
                "action": rev.action,
                "previous_value": rev.previous_value,
                "new_value": rev.new_value,
                "changed_fields": rev.changed_fields,
                "reviewer_note": rev.reviewer_note,
                "revised_at": rev.revised_at.isoformat() if rev.revised_at else None
            }
            for rev in (fact.revisions or [])
        ]
    }


@router.get("/api/patients/{patient_id}/profile")
async def get_patient_profile_data(
    patient_id: str,
    db: Session = Depends(get_db)
):
    """
    Return complete patient profile data:
    - All 19 categories with status and facts
    - Grounded patient summary and staleness
    - WHO ICD-11 diagnosis mappings and candidate options
    - Document readability and processing job state
    """
    try:
        patient_uuid = uuid.UUID(patient_id)
    except ValueError:
        raise HTTPException(400, "Invalid patient ID format")

    patient = db.query(Patient).filter(Patient.id == patient_uuid).first()
    if not patient:
        raise HTTPException(404, "Patient not found")

    # Documents & readability
    docs = (
        db.query(Document)
        .filter(Document.patient_id == patient_uuid)
        .order_by(Document.created_at.asc())
        .all()
    )
    doc_name_map = {str(d.id): (
        d.original_filename or "Pasted Clinical Note") for d in docs}
    doc_summaries = []
    total_pages = 0
    unreadable_pages_total = 0

    pages_by_doc: dict[uuid.UUID, list[DocumentPage]] = {d.id: [] for d in docs}
    if docs:
        all_pages = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id.in_([d.id for d in docs]))
            .order_by(DocumentPage.page_number.asc())
            .all()
        )
        for p in all_pages:
            pages_by_doc.setdefault(p.document_id, []).append(p)

    for d in docs:
        pages = pages_by_doc.get(d.id, [])
        total_pages += len(pages)
        unreadable_nums = [p.page_number for p in pages if not p.is_readable]
        unreadable_pages_total += len(unreadable_nums)
        warnings = []
        for p in pages:
            for w in (p.warnings or []):
                warnings.append(f"Page {p.page_number}: {w}")
        doc_summaries.append({
            "id": str(d.id),
            "original_filename": d.original_filename or "Pasted Clinical Note",
            "content_type": d.content_type,
            "file_size_bytes": d.file_size_bytes,
            "source_type": d.source_type,
            "upload_status": d.upload_status,
            "parsing_status": d.parsing_status,
            "page_count": len(pages),
            "readable_page_count": len(pages) - len(unreadable_nums),
            "unreadable_pages": unreadable_nums,
            "warnings": warnings,
            "created_at": d.created_at.isoformat() if d.created_at else None
        })

    # Latest processing job
    latest_job = (
        db.query(ProcessingJob)
        .filter(ProcessingJob.patient_id == patient_uuid)
        .order_by(ProcessingJob.created_at.desc())
        .first()
    )
    job_Stages = (latest_job.stages or {}) if latest_job else {}

    # Facts grouped across all 19 categories (preload revisions in 1 query instead of N queries)
    facts = (
        db.query(ConsolidatedFact)
        .options(selectinload(ConsolidatedFact.revisions))
        .filter(ConsolidatedFact.patient_id == patient_uuid)
        .order_by(
            ConsolidatedFact.category_number.asc(),
            ConsolidatedFact.entry_id.asc(),
            ConsolidatedFact.field_name.asc()
        )
        .all()
    )

    facts_by_cat: dict[int, list[ConsolidatedFact]] = {
        num: [] for num in CATEGORY_DEFINITIONS.keys()}
    for f in facts:
        if f.category_number in facts_by_cat:
            facts_by_cat[f.category_number].append(f)

    # Deduplicate Category 11 (Diagnosis) entries that share the same normalized diagnosis name,
    # assertion, and experiencer (e.g., chronic conditions extracted as both 'historical' and 'current')
    if facts_by_cat.get(11):
        cat11_by_entry: dict[str, list[ConsolidatedFact]] = {}
        for f in facts_by_cat[11]:
            cat11_by_entry.setdefault(f.entry_id, []).append(f)
        chosen_entry_by_sig: dict[tuple[str, str, str], tuple[tuple[int, int, int], str]] = {}
        non_diag_entries: list[str] = []
        for eid, efacts in cat11_by_entry.items():
            df = next((x for x in efacts if x.field_name == "diagnosis" and x.availability == "documented"), None)
            if not df or not df.value:
                non_diag_entries.append(eid)
                continue
            norm_diag = " ".join(df.value.strip().lower().split())
            sig = (
                norm_diag,
                (df.assertion or "present").lower(),
                (df.experiencer or "patient").lower(),
            )
            score = (
                1 if df.origin in ("reviewer_addition", "reviewer_correction") else 0,
                1 if (df.temporality or "current").lower() == "current" else 0,
                1 if df.review_status in ("confirmed", "corrected") else 0,
            )
            if sig not in chosen_entry_by_sig or score > chosen_entry_by_sig[sig][0]:
                chosen_entry_by_sig[sig] = (score, eid)
        kept_eids = {eid for _, eid in chosen_entry_by_sig.values()} | set(non_diag_entries)
        facts_by_cat[11] = [f for f in facts_by_cat[11] if f.entry_id in kept_eids]

    categories_payload = []
    for cat_num in sorted(CATEGORY_DEFINITIONS.keys()):
        defn = CATEGORY_DEFINITIONS[cat_num]
        group_letter = defn["group"]
        cat_facts = facts_by_cat.get(cat_num, [])

        documented_facts = [
            _serialize_fact(f, doc_name_map)
            for f in cat_facts
            if f.availability == "documented"
        ]
        has_not_documented_row = any(
            f.availability == "not_documented" for f in cat_facts)
        has_unreadable_row = any(
            f.availability == "unreadable" for f in cat_facts)
        group_stage_status = job_Stages.get(f"extraction_{group_letter}")

        if documented_facts:
            cat_status = "documented"
        elif group_stage_status == "failed":
            cat_status = "extraction_failed"
        elif has_unreadable_row or (total_pages > 0 and unreadable_pages_total == total_pages):
            cat_status = "unreadable"
        elif has_not_documented_row or (latest_job and latest_job.status in ("completed", "completed_with_warnings")):
            cat_status = "not_documented"
        else:
            cat_status = "not_analyzed"

        categories_payload.append({
            "category_number": cat_num,
            "key": defn["key"],
            "display_name": defn["display_name"],
            "group": group_letter,
            "status": cat_status,
            "subfields": defn["subfields"],
            "facts": documented_facts
        })

    # Latest summary
    summary = (
        db.query(PatientSummary)
        .filter(PatientSummary.patient_id == patient_uuid)
        .order_by(PatientSummary.generated_at.desc())
        .first()
    )
    summary_payload = None
    if summary:
        summary_payload = {
            "id": str(summary.id),
            "summary_text": summary.summary_text,
            "sentences": summary.sentences or [],
            "status": summary.status,
            "is_stale": summary.is_stale,
            "validation_errors": summary.validation_errors or [],
            "generated_at": summary.generated_at.isoformat() if summary.generated_at else None
        }

    # Diagnosis facts (Category 11) and WHO ICD-11 candidates + decisions (batch-loaded)
    diag_facts = [
        f for f in facts_by_cat.get(11, [])
        if f.field_name == "diagnosis" and f.availability == "documented"
    ]
    decisions_by_fact: dict[uuid.UUID, list[ICDDecision]] = {}
    candidates_by_norm_q: dict[str, list[ICDCandidate]] = {}
    candidates_by_diag_text: dict[str, list[ICDCandidate]] = {}

    if diag_facts:
        diag_ids = [df.id for df in diag_facts]
        all_decisions = (
            db.query(ICDDecision)
            .options(joinedload(ICDDecision.candidate))
            .filter(ICDDecision.fact_id.in_(diag_ids))
            .all()
        )
        for dec in all_decisions:
            decisions_by_fact.setdefault(dec.fact_id, []).append(dec)

        norm_queries: set[str] = set()
        diag_values: set[str] = set()
        for df in diag_facts:
            decs = decisions_by_fact.get(df.id, [])
            p_dec = decs[0] if decs else None
            q_str = (
                (p_dec.refined_query if p_dec and p_dec.refined_query else None)
                or df.value
                or ""
            )
            nq = who_icd_service.sanitize_diagnosis_query(q_str).lower()
            if nq:
                norm_queries.add(nq)
            if df.value:
                diag_values.add(df.value)

        filters = []
        if norm_queries:
            filters.append(ICDCandidate.normalized_query.in_(norm_queries))
        if diag_values:
            filters.append(ICDCandidate.diagnosis_text.in_(diag_values))

        if filters:
            all_cands = (
                db.query(ICDCandidate)
                .filter(or_(*filters))
                .order_by(ICDCandidate.search_rank.asc())
                .all()
            )
            for c in all_cands:
                if c.normalized_query:
                    candidates_by_norm_q.setdefault(c.normalized_query, []).append(c)
                if c.diagnosis_text:
                    candidates_by_diag_text.setdefault(c.diagnosis_text, []).append(c)

    icd_mappings = []
    for df in diag_facts:
        decisions = decisions_by_fact.get(df.id, [])
        primary_decision = decisions[0] if decisions else None

        query_str = (
            (primary_decision.refined_query if primary_decision and primary_decision.refined_query else None)
            or df.value
            or ""
        )
        norm_q = who_icd_service.sanitize_diagnosis_query(query_str).lower()

        candidates = list(candidates_by_norm_q.get(norm_q, []))
        if not candidates and df.value:
            candidates = list(candidates_by_diag_text.get(df.value, []))
        if primary_decision and primary_decision.candidate:
            existing_ids = {c.id for c in candidates}
            if primary_decision.candidate.id not in existing_ids:
                candidates = [primary_decision.candidate] + list(candidates)

        is_eligible = (
            df.review_status != "rejected"
            and (df.assertion or "present") != "negated"
            and (df.experiencer or "patient") == "patient"
        )

        icd_mappings.append({
            "fact_id": str(df.id),
            "entry_id": df.entry_id,
            "diagnosis": df.value,
            "assertion": df.assertion or "present",
            "temporality": df.temporality or "current",
            "experiencer": df.experiencer or "patient",
            "review_status": df.review_status,
            "is_eligible_for_icd": is_eligible,
            "ineligible_reason": (
                None if is_eligible else (
                    "Rejected fact" if df.review_status == "rejected"
                    else "Negated condition (excluded from ICD coding)" if df.assertion == "negated"
                    else f"Experiencer is {df.experiencer} (not patient diagnosis)"
                )
            ),
            "decision": {
                "id": str(primary_decision.id),
                "mapping_status": primary_decision.mapping_status,
                "needs_review": primary_decision.needs_review,
                "candidate_id": str(primary_decision.candidate_id) if primary_decision.candidate_id else None,
                "selected_code": primary_decision.candidate.mms_code if primary_decision and primary_decision.candidate else None,
                "selected_title": primary_decision.candidate.mms_title if primary_decision and primary_decision.candidate else None,
                "refined_query": primary_decision.refined_query,
                "reviewer_note": primary_decision.reviewer_note,
                "icd_release": primary_decision.icd_release,
                "decided_at": primary_decision.decided_at.isoformat() if primary_decision.decided_at else None
            } if primary_decision else None,
            "candidates": [
                {
                    "id": str(c.id),
                    "mms_code": c.mms_code,
                    "mms_title": c.mms_title,
                    "mms_description": c.mms_description,
                    "foundation_uri": c.foundation_uri,
                    "who_source_link": c.who_source_link,
                    "icd_release": c.icd_release,
                    "search_rank": c.search_rank
                }
                for c in candidates
            ]
        })

    return JSONResponse({
        "patient": {
            "id": str(patient.id),
            "display_name": patient.display_name,
            "is_demo": patient.is_demo,
            "created_at": patient.created_at.isoformat() if patient.created_at else None
        },
        "readiness": {
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "llm_configured": settings.is_llm_configured,
            "who_icd_configured": settings.is_who_icd_configured,
            "storage_configured": settings.is_storage_configured,
            "tesseract_configured": settings.is_tesseract_configured
        },
        "documents": doc_summaries,
        "job": {
            "id": str(latest_job.id),
            "status": latest_job.status,
            "stages": latest_job.stages or {},
            "current_stage": latest_job.current_stage,
            "error_message": latest_job.error_message,
            "started_at": latest_job.started_at.isoformat() if latest_job.started_at else None,
            "completed_at": latest_job.completed_at.isoformat() if latest_job.completed_at else None
        } if latest_job else None,
        "categories": categories_payload,
        "summary": summary_payload,
        "icd_mappings": icd_mappings
    })


@router.patch("/facts/{fact_id}")
async def update_fact(
    fact_id: str,
    payload: FactUpdate,
    db: Session = Depends(get_db)
):
    """
    Edit, confirm, or reject a clinical fact.
    - Preserves revision history in ReviewRevision.
    - Marks patient summaries as stale.
    - Invalidates accepted ICD decisions with needs_review=True when a diagnosis is edited.
    """
    try:
        fact_uuid = uuid.UUID(fact_id)
    except ValueError:
        raise HTTPException(400, "Invalid fact ID format")

    fact = db.query(ConsolidatedFact).filter(
        ConsolidatedFact.id == fact_uuid).first()
    if not fact:
        raise HTTPException(404, "Clinical fact not found")

    previous_value = fact.value
    changed_fields = []

    if payload.value is not None and payload.value != fact.value:
        fact.value = payload.value
        changed_fields.append("value")

    if payload.unit is not None:
        vjson = dict(fact.value_json) if isinstance(
            fact.value_json, dict) else {}
        if vjson.get("unit") != payload.unit:
            vjson["unit"] = payload.unit
            fact.value_json = vjson
            changed_fields.append("unit")

    if payload.availability is not None and payload.availability != fact.availability:
        fact.availability = payload.availability
        changed_fields.append("availability")

    if payload.assertion is not None and payload.assertion != fact.assertion:
        fact.assertion = payload.assertion
        changed_fields.append("assertion")

    if payload.temporality is not None and payload.temporality != fact.temporality:
        fact.temporality = payload.temporality
        changed_fields.append("temporality")

    if payload.experiencer is not None and payload.experiencer != fact.experiencer:
        fact.experiencer = payload.experiencer
        changed_fields.append("experiencer")

    if payload.event_time is not None and payload.event_time != fact.event_time:
        fact.event_time = payload.event_time
        changed_fields.append("event_time")

    # Determine review status and origin
    if changed_fields:
        if fact.origin == "extraction":
            fact.origin = "reviewer_correction"
        fact.review_status = payload.review_status or "corrected"
        action = "edit"
    elif payload.review_status:
        fact.review_status = payload.review_status
        action = "reject" if payload.review_status == "rejected" else "confirm"
    else:
        action = "confirm"
        fact.review_status = "confirmed"

    fact.reviewed_at = datetime.utcnow()

    revision = ReviewRevision(
        id=uuid.uuid4(),
        fact_id=fact.id,
        action=action,
        previous_value=previous_value,
        new_value=fact.value,
        changed_fields=changed_fields,
        reviewer_note=payload.reviewer_note,
        revised_at=datetime.utcnow()
    )
    db.add(revision)

    # Mark patient summaries as stale
    db.query(PatientSummary).filter(
        PatientSummary.patient_id == fact.patient_id
    ).update({"is_stale": True})

    # If diagnosis fact (category 11) was edited or rejected, invalidate accepted ICD mappings
    if fact.category_number == 11 and fact.field_name == "diagnosis":
        if changed_fields or fact.review_status == "rejected":
            decisions = db.query(ICDDecision).filter(
                ICDDecision.fact_id == fact.id).all()
            for dec in decisions:
                dec.needs_review = True
                dec.invalidated_at = datetime.utcnow()
                if fact.review_status == "rejected" or fact.assertion == "negated" or fact.experiencer != "patient":
                    dec.mapping_status = "rejected"

    db.commit()
    db.refresh(fact)

    return JSONResponse({
        "success": True,
        "fact": _serialize_fact(fact, {})
    })


@router.post("/facts/batch-review")
async def batch_review_facts(
    payload: BatchFactReviewRequest,
    db: Session = Depends(get_db)
):
    """Batch confirm or reject multiple clinical facts in a single database transaction."""
    if not payload.fact_ids:
        return JSONResponse({"success": True, "updated_count": 0})

    uuids = []
    for fid in payload.fact_ids:
        try:
            uuids.append(uuid.UUID(fid))
        except ValueError:
            continue

    if not uuids:
        raise HTTPException(400, "No valid fact IDs provided")

    facts = db.query(ConsolidatedFact).filter(ConsolidatedFact.id.in_(uuids)).all()
    if not facts:
        return JSONResponse({"success": True, "updated_count": 0})

    now = datetime.utcnow()
    action = "reject" if payload.review_status == "rejected" else "confirm"
    patient_ids = set()
    rejected_diag_ids = []

    for fact in facts:
        fact.review_status = payload.review_status
        fact.reviewed_at = now
        patient_ids.add(fact.patient_id)
        db.add(ReviewRevision(
            id=uuid.uuid4(),
            fact_id=fact.id,
            action=action,
            previous_value=fact.value,
            new_value=fact.value,
            changed_fields=[],
            reviewer_note=payload.reviewer_note,
            revised_at=now
        ))
        if fact.category_number == 11 and fact.field_name == "diagnosis" and payload.review_status == "rejected":
            rejected_diag_ids.append(fact.id)

    if patient_ids:
        db.query(PatientSummary).filter(
            PatientSummary.patient_id.in_(patient_ids)
        ).update({"is_stale": True}, synchronize_session=False)

    if rejected_diag_ids:
        db.query(ICDDecision).filter(
            ICDDecision.fact_id.in_(rejected_diag_ids)
        ).update({
            "needs_review": True,
            "invalidated_at": now,
            "mapping_status": "rejected"
        }, synchronize_session=False)

    db.commit()
    return JSONResponse({
        "success": True,
        "updated_count": len(facts)
    })


@router.post("/icd/batch-decision")
async def batch_save_icd_decisions(
    payload: BatchICDDecisionRequest,
    db: Session = Depends(get_db)
):
    """Batch accept/update multiple WHO ICD-11 decisions in a single database transaction."""
    if not payload.decisions:
        return JSONResponse({"success": True, "updated_count": 0})

    fact_uuids = []
    items_by_fact: dict[uuid.UUID, BatchICDDecisionItem] = {}
    for item in payload.decisions:
        try:
            f_uuid = uuid.UUID(item.fact_id)
            uuid.UUID(item.candidate_id)
            fact_uuids.append(f_uuid)
            items_by_fact[f_uuid] = item
        except ValueError:
            continue

    if not fact_uuids:
        return JSONResponse({"success": True, "updated_count": 0})

    existing_decisions = db.query(ICDDecision).filter(
        ICDDecision.fact_id.in_(fact_uuids)
    ).all()
    existing_by_fact = {d.fact_id: d for d in existing_decisions}

    now = datetime.utcnow()
    for f_uuid, item in items_by_fact.items():
        c_uuid = uuid.UUID(item.candidate_id)
        dec = existing_by_fact.get(f_uuid)
        if not dec:
            db.add(ICDDecision(
                id=uuid.uuid4(),
                fact_id=f_uuid,
                candidate_id=c_uuid,
                mapping_status=item.mapping_status,
                needs_review=False,
                reviewer_note=item.reviewer_note,
                icd_release=settings.who_icd_release,
                icd_language=settings.who_icd_language,
                decided_at=now
            ))
        else:
            dec.candidate_id = c_uuid
            dec.mapping_status = item.mapping_status
            dec.needs_review = False
            dec.reviewer_note = item.reviewer_note
            dec.decided_at = now

    db.commit()
    return JSONResponse({
        "success": True,
        "updated_count": len(items_by_fact)
    })


@router.post("/facts/patient/{patient_id}")
async def add_manual_fact(
    patient_id: str,
    payload: FactCreate,
    db: Session = Depends(get_db)
):
    """Manually add a clinical fact to a patient profile with reviewer provenance."""
    try:
        patient_uuid = uuid.UUID(patient_id)
    except ValueError:
        raise HTTPException(400, "Invalid patient ID format")

    patient = db.query(Patient).filter(Patient.id == patient_uuid).first()
    if not patient:
        raise HTTPException(404, "Patient not found")

    if payload.category_number not in CATEGORY_DEFINITIONS:
        raise HTTPException(
            400, f"Invalid category_number {payload.category_number} (must be 1..19)")

    cat_def = CATEGORY_DEFINITIONS[payload.category_number]
    cat_key = cat_def["key"]

    # Remove any placeholder 'not_documented' row for this category
    db.query(ConsolidatedFact).filter(
        ConsolidatedFact.patient_id == patient_uuid,
        ConsolidatedFact.category_number == payload.category_number,
        ConsolidatedFact.availability == "not_documented"
    ).delete()

    evidence_list = []
    if payload.document_id or payload.quote:
        evidence_list.append({
            "document_id": payload.document_id or "manual_entry",
            "page_number": payload.page_number or 1,
            "block_id": payload.block_id,
            "quote": payload.quote or payload.reviewer_note or "Reviewer manual addition"
        })

    entry_id = payload.entry_id or f"{cat_key}_manual_{uuid.uuid4().hex[:6]}"
    fact = ConsolidatedFact(
        id=uuid.uuid4(),
        patient_id=patient_uuid,
        category_number=payload.category_number,
        category_key=cat_key,
        entry_id=entry_id,
        field_name=payload.field_name.strip(),
        value=payload.value.strip(),
        value_json={"unit": payload.unit, "snapshot_id": None},
        availability="documented",
        assertion=payload.assertion or "present",
        temporality=payload.temporality or "current",
        experiencer=payload.experiencer or "patient",
        event_time=payload.event_time,
        original_time_expression=payload.event_time,
        evidence=evidence_list,
        origin="reviewer_addition",
        review_status="confirmed",
        extracted_at=datetime.utcnow(),
        reviewed_at=datetime.utcnow()
    )
    db.add(fact)
    db.flush()

    revision = ReviewRevision(
        id=uuid.uuid4(),
        fact_id=fact.id,
        action="add",
        previous_value=None,
        new_value=fact.value,
        changed_fields=[fact.field_name],
        reviewer_note=payload.reviewer_note or "Added by reviewer",
        revised_at=datetime.utcnow()
    )
    db.add(revision)

    # If adding a diagnosis in Category 11, handle manual ICD-11 code or auto WHO lookup
    if fact.category_number == 11 and fact.field_name == "diagnosis":
        norm_query = who_icd_service.sanitize_diagnosis_query(fact.value or "").lower() or "manual"
        if payload.manual_icd_code and payload.manual_icd_code.strip():
            m_code = payload.manual_icd_code.strip()
            m_title = (payload.manual_icd_title or fact.value or "Manual ICD-11 Entry").strip()
            cand = db.query(ICDCandidate).filter(
                ICDCandidate.mms_code == m_code,
                ICDCandidate.normalized_query == norm_query
            ).first()
            if not cand:
                cand = ICDCandidate(
                    id=uuid.uuid4(),
                    diagnosis_text=fact.value,
                    normalized_query=norm_query,
                    mms_code=m_code,
                    mms_title=m_title,
                    icd_release=settings.who_icd_release,
                    icd_language=settings.who_icd_language,
                    search_rank=0
                )
                db.add(cand)
                db.flush()
            else:
                cand.mms_title = m_title
            dec = ICDDecision(
                id=uuid.uuid4(),
                fact_id=fact.id,
                candidate_id=cand.id,
                mapping_status="accepted",
                needs_review=False,
                reviewer_note="Manual diagnosis and ICD-11 entry",
                icd_release=settings.who_icd_release,
                icd_language=settings.who_icd_language,
                decided_at=datetime.utcnow()
            )
            db.add(dec)
        elif (
            payload.auto_icd_lookup
            and (fact.assertion or "present") != "negated"
            and (fact.experiencer or "patient") == "patient"
            and who_icd_service.is_configured()
        ):
            try:
                candidates_list = await who_icd_service.search_diagnosis(fact.value, max_results=5)
                saved_candidates = []
                for cand_data in candidates_list:
                    cand = db.query(ICDCandidate).filter(
                        ICDCandidate.mms_code == cand_data["mms_code"],
                        ICDCandidate.normalized_query == norm_query,
                        ICDCandidate.icd_release == settings.who_icd_release
                    ).first()
                    if not cand:
                        cand = ICDCandidate(
                            id=uuid.uuid4(),
                            diagnosis_text=fact.value,
                            normalized_query=norm_query,
                            mms_code=cand_data["mms_code"],
                            mms_title=cand_data["mms_title"],
                            mms_description=cand_data.get("mms_description"),
                            foundation_uri=cand_data.get("foundation_uri"),
                            who_source_link=cand_data.get("who_source_link"),
                            icd_release=settings.who_icd_release,
                            icd_language=settings.who_icd_language,
                            search_rank=cand_data["search_rank"]
                        )
                        db.add(cand)
                        db.flush()
                    saved_candidates.append(cand)
                dec = ICDDecision(
                    id=uuid.uuid4(),
                    fact_id=fact.id,
                    candidate_id=saved_candidates[0].id if saved_candidates else None,
                    mapping_status="suggested" if saved_candidates else "unresolved",
                    needs_review=False,
                    icd_release=settings.who_icd_release,
                    icd_language=settings.who_icd_language,
                    suggested_at=datetime.utcnow()
                )
                db.add(dec)
            except Exception as e:
                logger.warning(f"Auto WHO ICD lookup failed for manual diagnosis '{fact.value}': {e}")

    # Mark summaries stale
    db.query(PatientSummary).filter(
        PatientSummary.patient_id == patient_uuid
    ).update({"is_stale": True})

    db.commit()
    db.refresh(fact)

    return JSONResponse({
        "success": True,
        "fact": _serialize_fact(fact, {})
    })


@router.delete("/facts/{fact_id}")
async def delete_fact(
    fact_id: str,
    delete_entry: bool = Query(True, description="Delete all subfield facts in the same entry group"),
    db: Session = Depends(get_db)
):
    """Permanently delete a clinical fact (or its entire entry group) and any associated ICD decisions."""
    try:
        fact_uuid = uuid.UUID(fact_id)
    except ValueError:
        raise HTTPException(400, "Invalid fact ID format")

    fact = db.query(ConsolidatedFact).filter(
        ConsolidatedFact.id == fact_uuid).first()
    if not fact:
        raise HTTPException(404, "Fact not found")

    patient_id = fact.patient_id
    if delete_entry and fact.entry_id:
        target_facts = db.query(ConsolidatedFact).filter(
            ConsolidatedFact.patient_id == patient_id,
            ConsolidatedFact.entry_id == fact.entry_id
        ).all()
    else:
        target_facts = [fact]

    target_ids = [f.id for f in target_facts]

    # Delete associated ICD decisions and review revisions
    db.query(ICDDecision).filter(ICDDecision.fact_id.in_(target_ids)).delete(synchronize_session=False)
    db.query(ReviewRevision).filter(ReviewRevision.fact_id.in_(target_ids)).delete(synchronize_session=False)
    for tf in target_facts:
        db.delete(tf)

    # Mark patient summaries stale
    db.query(PatientSummary).filter(
        PatientSummary.patient_id == patient_id
    ).update({"is_stale": True})

    db.commit()

    return JSONResponse({
        "success": True,
        "deleted_count": len(target_ids)
    })


@router.post("/icd/decision/{fact_id}")
async def save_icd_decision(
    fact_id: str,
    payload: ICDDecisionUpdate,
    db: Session = Depends(get_db)
):
    """Accept, reject, or update a WHO ICD-11 mapping decision for a diagnosis fact."""
    try:
        fact_uuid = uuid.UUID(fact_id)
    except ValueError:
        raise HTTPException(400, "Invalid fact ID format")

    fact = db.query(ConsolidatedFact).filter(
        ConsolidatedFact.id == fact_uuid).first()
    if not fact:
        raise HTTPException(404, "Diagnosis fact not found")

    if payload.mapping_status not in {"accepted", "rejected", "unresolved", "suggested"}:
        raise HTTPException(400, "Invalid mapping_status")

    candidate_uuid = None
    if payload.manual_code and payload.manual_code.strip():
        m_code = payload.manual_code.strip()
        m_title = (payload.manual_title or fact.value or "Manual ICD-11 Entry").strip()
        norm_query = who_icd_service.sanitize_diagnosis_query(fact.value or "").lower() or "manual"
        cand = db.query(ICDCandidate).filter(
            ICDCandidate.mms_code == m_code,
            ICDCandidate.normalized_query == norm_query
        ).first()
        if not cand:
            cand = ICDCandidate(
                id=uuid.uuid4(),
                diagnosis_text=fact.value or m_title,
                normalized_query=norm_query,
                mms_code=m_code,
                mms_title=m_title,
                icd_release=settings.who_icd_release,
                icd_language=settings.who_icd_language,
                search_rank=0
            )
            db.add(cand)
            db.flush()
        else:
            cand.mms_title = m_title
        candidate_uuid = cand.id
    elif payload.candidate_id:
        try:
            candidate_uuid = uuid.UUID(payload.candidate_id)
        except ValueError:
            raise HTTPException(400, "Invalid candidate ID format")

    decision = db.query(ICDDecision).filter(
        ICDDecision.fact_id == fact.id).first()
    if not decision:
        decision = ICDDecision(
            id=uuid.uuid4(),
            fact_id=fact.id,
            candidate_id=candidate_uuid,
            mapping_status=payload.mapping_status,
            needs_review=False,
            reviewer_note=payload.reviewer_note,
            icd_release=settings.who_icd_release,
            icd_language=settings.who_icd_language,
            decided_at=datetime.utcnow()
        )
        db.add(decision)
    else:
        if candidate_uuid is not None:
            decision.candidate_id = candidate_uuid
        elif payload.mapping_status in {"rejected", "unresolved"}:
            decision.candidate_id = None
        decision.mapping_status = payload.mapping_status
        decision.needs_review = False
        decision.reviewer_note = payload.reviewer_note
        decision.decided_at = datetime.utcnow()

    db.commit()
    db.refresh(decision)

    return JSONResponse({
        "success": True,
        "decision": {
            "id": str(decision.id),
            "fact_id": str(decision.fact_id),
            "candidate_id": str(decision.candidate_id) if decision.candidate_id else None,
            "mapping_status": decision.mapping_status,
            "needs_review": decision.needs_review,
            "reviewer_note": decision.reviewer_note,
            "decided_at": decision.decided_at.isoformat() if decision.decided_at else None
        }
    })


@router.post("/icd/lookup/{fact_id}")
async def lookup_icd_for_fact(
    fact_id: str,
    payload: Optional[ICDLookupRequest] = None,
    db: Session = Depends(get_db)
):
    """Retry or refine WHO ICD-11 MMS search for a single diagnosis fact."""
    try:
        fact_uuid = uuid.UUID(fact_id)
    except ValueError:
        raise HTTPException(400, "Invalid fact ID format")

    fact = db.query(ConsolidatedFact).filter(
        ConsolidatedFact.id == fact_uuid).first()
    if not fact:
        raise HTTPException(404, "Diagnosis fact not found")

    if (fact.assertion or "present") == "negated" or (fact.experiencer or "patient") != "patient":
        return JSONResponse({
            "success": False,
            "error": "Negated or non-patient (family history) conditions cannot be mapped to patient ICD-11 codes."
        }, status_code=400)

    search_query = (
        payload.refined_query if payload and payload.refined_query else fact.value or "").strip()
    if not search_query:
        return JSONResponse({"success": False, "error": "Diagnosis search query is empty"}, status_code=400)

    if not who_icd_service.is_configured():
        return JSONResponse({
            "success": False,
            "error": "WHO ICD-11 credentials (WHO_ICD_CLIENT_ID / WHO_ICD_CLIENT_SECRET) are not configured in .env."
        }, status_code=400)

    try:
        candidates_list = await who_icd_service.search_diagnosis(search_query, max_results=5)
        norm_query = who_icd_service.sanitize_diagnosis_query(
            search_query).lower()

        saved_candidates = []
        for cand_data in candidates_list:
            cand = db.query(ICDCandidate).filter(
                ICDCandidate.mms_code == cand_data["mms_code"],
                ICDCandidate.normalized_query == norm_query,
                ICDCandidate.icd_release == settings.who_icd_release
            ).first()
            if not cand:
                cand = ICDCandidate(
                    id=uuid.uuid4(),
                    diagnosis_text=search_query,
                    normalized_query=norm_query,
                    mms_code=cand_data["mms_code"],
                    mms_title=cand_data["mms_title"],
                    mms_description=cand_data.get("mms_description"),
                    foundation_uri=cand_data.get("foundation_uri"),
                    who_source_link=cand_data.get("who_source_link"),
                    icd_release=settings.who_icd_release,
                    icd_language=settings.who_icd_language,
                    search_rank=cand_data["search_rank"]
                )
                db.add(cand)
                db.flush()
            saved_candidates.append(cand)

        decision = db.query(ICDDecision).filter(
            ICDDecision.fact_id == fact.id).first()
        if not decision:
            decision = ICDDecision(
                id=uuid.uuid4(),
                fact_id=fact.id,
                candidate_id=saved_candidates[0].id if saved_candidates else None,
                mapping_status="suggested" if saved_candidates else "unresolved",
                needs_review=False,
                refined_query=search_query if search_query != fact.value else None,
                icd_release=settings.who_icd_release,
                icd_language=settings.who_icd_language,
                suggested_at=datetime.utcnow()
            )
            db.add(decision)
        else:
            decision.refined_query = search_query if search_query != fact.value else decision.refined_query
            decision.candidate_id = saved_candidates[0].id if saved_candidates else None
            decision.mapping_status = "suggested" if saved_candidates else "unresolved"
            decision.needs_review = False
            decision.suggested_at = datetime.utcnow()

        db.commit()

        return JSONResponse({
            "success": True,
            "query": search_query,
            "candidates": [
                {
                    "id": str(c.id),
                    "mms_code": c.mms_code,
                    "mms_title": c.mms_title,
                    "mms_description": c.mms_description,
                    "who_source_link": c.who_source_link,
                    "search_rank": c.search_rank
                }
                for c in saved_candidates
            ]
        })

    except WHOICDError as e:
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=502)


@router.post("/profile/{patient_id}/save")
async def save_patient_profile(
    patient_id: str,
    db: Session = Depends(get_db)
):
    """Save and finalize the patient clinical profile and return redirect to patient list."""
    try:
        patient_uuid = uuid.UUID(patient_id)
    except ValueError:
        raise HTTPException(400, "Invalid patient ID format")

    patient = db.query(Patient).filter(Patient.id == patient_uuid).first()
    if not patient:
        raise HTTPException(404, "Patient not found")

    patient.updated_at = datetime.utcnow()
    db.commit()

    return JSONResponse({
        "success": True,
        "patient_id": str(patient.id),
        "redirect_url": "/patients"
    })
