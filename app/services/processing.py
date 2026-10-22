"""Processing orchestration service with persistent stage states."""
import logging
import uuid
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
from app.config import settings
from app.models import (
    Patient, Document, DocumentPage, ProcessingJob, ExtractionSnapshot,
    ExtractionRun, ConsolidatedFact, PatientSummary, ICDCandidate, ICDDecision
)
from app.schemas.clinical import CATEGORY_DEFINITIONS, CATEGORY_GROUPS
from app.services.extraction import ExtractionService, ExtractionError
from app.services.patient_summary import patient_summary_service
from app.services.who_icd import who_icd_service, WHOICDError

logger = logging.getLogger(__name__)

ACTIVE_JOB_STATUSES = {"pending", "parsing",
                       "extracting", "summarizing", "icd_lookup"}
METADATA_KEYS = {
    "availability", "assertion", "temporality", "experiencer",
    "event_time", "original_time_expression", "origin", "evidence", "unit"
}


def recover_stale_jobs(db: Session, force_all_active: bool = False) -> int:
    """
    Mark active jobs whose lease has expired (or all active jobs on startup)
    as 'interrupted' (or 'completed' if facts & summary already finished) so
    they never stay stuck or block new patient analyses.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=settings.job_lease_timeout_minutes)
    query = db.query(ProcessingJob).filter(
        ProcessingJob.status.in_(ACTIVE_JOB_STATUSES))
    if not force_all_active:
        query = query.filter(ProcessingJob.updated_at < cutoff)

    stale_jobs = query.all()
    for job in stale_jobs:
        stages = dict(job.stages or {})
        # If facts and summary already succeeded and the job was only in the final ICD lookup stage
        # when Render restarted the container, mark it completed instead of interrupted.
        if stages.get("facts") == "completed" and stages.get("summary") == "completed":
            if stages.get("icd") == "running":
                stages["icd"] = "completed"
            job.stages = stages
            job.status = "completed"
            job.current_stage = None
            job.error_message = None
            job.completed_at = job.completed_at or datetime.utcnow()
        else:
            job.status = "interrupted"
            job.current_stage = None
            job.error_message = "Job was interrupted by server restart or timeout. Click 'Retry Failed Stages' or 'Run Full Analysis'."
        job.updated_at = datetime.utcnow()

    if stale_jobs:
        db.commit()
        logger.info(
            f"Recovered {len(stale_jobs)} stale/interrupted processing job(s)")
    return len(stale_jobs)


def get_active_job(db: Session, patient_id: Optional[uuid.UUID] = None) -> Optional[ProcessingJob]:
    """Return currently active processing job (scoped to patient_id if provided) after recovering expired leases."""
    recover_stale_jobs(db, force_all_active=False)
    query = db.query(ProcessingJob).filter(ProcessingJob.status.in_(ACTIVE_JOB_STATUSES))
    if patient_id is not None:
        query = query.filter(ProcessingJob.patient_id == patient_id)
    return query.order_by(ProcessingJob.created_at.desc()).first()


class ProcessingService:
    """Orchestrates document processing workflow."""

    def __init__(self, db: Session):
        """Initialize with database session."""
        self.db = db
        self.extraction_service = ExtractionService(db)

    async def process_patient_documents(
        self,
        patient_id: uuid.UUID,
        document_ids: List[uuid.UUID],
        allow_partial_readable: bool = False,
        existing_job_id: Optional[uuid.UUID] = None
    ) -> ProcessingJob:
        """
        Process patient documents through complete workflow:
        1. Snapshot creation
        2. Parallel extraction (Groups A, B, C)
        3. Fact consolidation (preserving reviewer revisions)
        4. Grounded patient summary generation
        5. WHO ICD-11 MMS lookup for diagnoses
        """
        if existing_job_id:
            job = self.db.query(ProcessingJob).filter(
                ProcessingJob.id == existing_job_id).first()
            if not job:
                raise ValueError(f"ProcessingJob {existing_job_id} not found")
        else:
            job = ProcessingJob(
                id=uuid.uuid4(),
                patient_id=patient_id,
                status="pending",
                stages={}
            )
            self.db.add(job)
            self.db.commit()
            self.db.refresh(job)

        logger.info(
            f"Starting processing job {job.id} for patient {patient_id}")

        try:
            stages = dict(job.stages or {})
            job.status = "extracting"
            job.current_stage = "snapshot"
            job.started_at = job.started_at or datetime.utcnow()
            job.error_message = None
            stages["extraction"] = "running"
            job.stages = stages
            self.db.commit()

            snapshot = await self.extraction_service.create_snapshot(
                patient_id=patient_id,
                document_ids=document_ids,
                job_id=job.id,
                allow_partial_readable=allow_partial_readable
            )

            stages = dict(job.stages or {})
            stages["snapshot"] = "completed"
            job.current_stage = "extracting_groups"
            job.stages = stages
            self.db.commit()

            combined_results, group_statuses = await self.extraction_service.run_parallel_extraction(
                snapshot=snapshot,
                job_id=job.id
            )

            stages = dict(job.stages or {})
            for g_name, g_status in group_statuses.items():
                stages[f"extraction_{g_name}"] = g_status

            any_group_failed = any(
                v == "failed" for v in group_statuses.values())
            stages["extraction"] = "partial" if any_group_failed else "completed"
            job.stages = stages
            self.db.commit()

            # Store facts from completed groups
            job.current_stage = "storing_facts"
            stages = dict(job.stages or {})
            stages["facts"] = "running"
            job.stages = stages
            self.db.commit()

            completed_groups = [
                g for g, s in group_statuses.items() if s == "completed"]
            fact_count = await self._store_facts(
                patient_id=patient_id,
                extraction_results=combined_results,
                snapshot_id=snapshot.id,
                completed_groups=completed_groups
            )
            logger.info(f"Stored {fact_count} facts for patient {patient_id}")

            stages = dict(job.stages or {})
            stages["facts"] = "completed"
            job.stages = stages
            self.db.commit()

            # Generate summary
            job.status = "summarizing"
            job.current_stage = "generating_summary"
            stages = dict(job.stages or {})
            stages["summary"] = "running"
            job.stages = stages
            self.db.commit()

            try:
                await self._generate_summary(patient_id, snapshot.id)
                stages = dict(job.stages or {})
                stages["summary"] = "completed"
            except Exception as e:
                logger.error(f"Summary generation failed: {e}")
                stages = dict(job.stages or {})
                stages["summary"] = "failed"

            job.stages = stages
            self.db.commit()

            # ICD lookup for diagnoses
            job.status = "icd_lookup"
            job.current_stage = "icd_lookup"
            stages = dict(job.stages or {})
            stages["icd"] = "running"
            job.stages = stages
            self.db.commit()

            try:
                icd_status = await self._lookup_icd_codes(patient_id)
                stages = dict(job.stages or {})
                stages["icd"] = icd_status
            except Exception as e:
                logger.error(f"ICD lookup failed: {e}")
                stages = dict(job.stages or {})
                stages["icd"] = "failed"

            job.stages = stages
            job.current_stage = None
            job.completed_at = datetime.utcnow()
            if any_group_failed:
                job.status = "completed_with_warnings"
                failed_list = [
                    g for g, s in group_statuses.items() if s == "failed"]
                job.error_message = f"Completed with failed extraction group(s): {', '.join(failed_list)}. You can retry failed groups."
            else:
                job.status = "completed"
            self.db.commit()

            logger.info(
                f"Processing job {job.id} finished with status {job.status}")
            return job

        except ExtractionError as e:
            logger.error(f"Extraction error: {e}")
            stages = dict(job.stages or {})
            stages["extraction"] = "failed"
            job.stages = stages
            job.status = "failed"
            job.current_stage = None
            job.error_message = str(e)
            self.db.commit()
            return job

        except Exception as e:
            logger.error(f"Processing failed: {e}")
            job.status = "failed"
            job.current_stage = None
            job.error_message = str(e)
            self.db.commit()
            return job

    async def retry_job(
        self,
        job_id: uuid.UUID,
        allow_partial_readable: bool = True
    ) -> ProcessingJob:
        """
        Retry failed or interrupted stages of an existing processing job without
        rerunning already-completed extraction groups on the same snapshot.
        """
        job = self.db.query(ProcessingJob).filter(
            ProcessingJob.id == job_id).first()
        if not job:
            raise ValueError(f"Job {job_id} not found")

        patient_id = job.patient_id
        snapshot = self.db.query(ExtractionSnapshot).filter(
            ExtractionSnapshot.job_id == job.id).first()

        if not snapshot:
            docs = self.db.query(Document).filter(
                Document.patient_id == patient_id,
                Document.parsing_status == "completed"
            ).all()
            doc_ids = [d.id for d in docs]
            return await self.process_patient_documents(
                patient_id=patient_id,
                document_ids=doc_ids,
                allow_partial_readable=allow_partial_readable,
                existing_job_id=job.id
            )

        try:
            stages = dict(job.stages or {})
            job.status = "extracting"
            job.error_message = None
            self.db.commit()

            runs = self.db.query(ExtractionRun).filter(
                ExtractionRun.job_id == job.id).all()
            completed_groups = {
                r.group_name for r in runs if r.status == "completed" and r.validated_output}
            failed_or_missing_groups = [
                g for g in CATEGORY_GROUPS.keys() if g not in completed_groups
            ]

            if failed_or_missing_groups:
                job.current_stage = "retrying_extraction"
                self.db.commit()

                combined_results, group_statuses = await self.extraction_service.run_parallel_extraction(
                    snapshot=snapshot,
                    job_id=job.id,
                    only_groups=failed_or_missing_groups
                )

                for g_name, g_status in group_statuses.items():
                    stages[f"extraction_{g_name}"] = g_status
                any_failed = any(
                    v == "failed" for v in group_statuses.values())
                stages["extraction"] = "partial" if any_failed else "completed"
                job.stages = dict(stages)
                self.db.commit()

                newly_completed = [
                    g for g in failed_or_missing_groups if group_statuses.get(g) == "completed"
                ]
                if newly_completed:
                    await self._store_facts(
                        patient_id=patient_id,
                        extraction_results=combined_results,
                        snapshot_id=snapshot.id,
                        completed_groups=newly_completed
                    )
                    stages["facts"] = "completed"
                    job.stages = dict(stages)
                    self.db.commit()

            if stages.get("summary") != "completed" or failed_or_missing_groups:
                job.status = "summarizing"
                job.current_stage = "generating_summary"
                self.db.commit()
                try:
                    await self._generate_summary(patient_id, snapshot.id)
                    stages["summary"] = "completed"
                except Exception as e:
                    logger.error(f"Retry summary failed: {e}")
                    stages["summary"] = "failed"
                job.stages = dict(stages)
                self.db.commit()

            if stages.get("icd") not in ("completed", "not_applicable") or failed_or_missing_groups:
                job.status = "icd_lookup"
                job.current_stage = "icd_lookup"
                self.db.commit()
                try:
                    icd_status = await self._lookup_icd_codes(patient_id)
                    stages["icd"] = icd_status
                except Exception as e:
                    logger.error(f"Retry ICD lookup failed: {e}")
                    stages["icd"] = "failed"
                job.stages = dict(stages)
                self.db.commit()

            job.current_stage = None
            job.completed_at = datetime.utcnow()
            any_group_failed = any(
                stages.get(f"extraction_{g}") == "failed" for g in CATEGORY_GROUPS.keys()
            )
            job.status = "completed_with_warnings" if any_group_failed else "completed"
            self.db.commit()
            return job

        except Exception as e:
            logger.error(f"Job retry failed: {e}")
            job.status = "failed"
            job.current_stage = None
            job.error_message = str(e)
            self.db.commit()
            return job

    async def _store_facts(
        self,
        patient_id: uuid.UUID,
        extraction_results: Dict[str, Any],
        snapshot_id: uuid.UUID,
        completed_groups: Optional[List[str]] = None
    ) -> int:
        """
        Store extracted facts in database while preserving reviewer edits/additions/confirmations.
        Also records explicit 'not_documented' category rows for empty processed categories.
        """
        fact_count = 0
        target_groups = completed_groups or list(CATEGORY_GROUPS.keys())
        target_category_nums = {
            cat_num
            for g in target_groups
            for cat_num in CATEGORY_GROUPS.get(g, [])
        }

        # Delete only unreviewed machine-extracted facts for the target categories
        existing_facts = self.db.query(ConsolidatedFact).filter(
            ConsolidatedFact.patient_id == patient_id,
            ConsolidatedFact.category_number.in_(target_category_nums)
        ).all()

        # Group existing facts by (category_number, entry_id) so entries are preserved/deleted as a unit
        from collections import defaultdict
        entries_by_key: Dict[Tuple[int, str], List[ConsolidatedFact]] = defaultdict(list)
        for existing in existing_facts:
            entries_by_key[(existing.category_number, existing.entry_id)].append(existing)

        preserved_signatures = set()
        preserved_entry_ids_by_cat: Dict[int, set] = defaultdict(set)
        preserved_fields_by_cat: Dict[int, set] = defaultdict(set)
        preserved_primary_vals_by_cat: Dict[int, set] = defaultdict(set)

        primary_keys = [
            "documented_name", "diagnosis", "name", "test_name", "measurement_name",
            "substance", "symptom", "condition", "procedure", "main_complaint",
            "action", "instruction", "event", "finding", "category"
        ]

        for (cat_num, entry_id), group_facts in entries_by_key.items():
            is_entry_touched = any(
                f.origin in ("reviewer_correction", "reviewer_addition")
                or f.review_status in ("confirmed", "corrected", "rejected")
                for f in group_facts
            )
            if is_entry_touched:
                preserved_entry_ids_by_cat[cat_num].add(entry_id)
                for f in group_facts:
                    norm_val = " ".join((f.value or "").strip().lower().split())
                    preserved_signatures.add((cat_num, f.field_name, norm_val))
                    preserved_fields_by_cat[cat_num].add(f.field_name)
                    if f.field_name in primary_keys and norm_val:
                        preserved_primary_vals_by_cat[cat_num].add((f.field_name, norm_val))
            else:
                for f in group_facts:
                    self.db.delete(f)

        self.db.flush()

        # Mark existing summaries stale since facts are being updated
        self.db.query(PatientSummary).filter(
            PatientSummary.patient_id == patient_id
        ).update({"is_stale": True})

        for cat_num in sorted(target_category_nums):
            cat_def = CATEGORY_DEFINITIONS[cat_num]
            cat_key = cat_def["key"]
            cat_data = extraction_results.get(cat_key)

            entries: List[Dict[str, Any]] = []
            if isinstance(cat_data, list):
                entries = cat_data
            elif isinstance(cat_data, dict):
                entries = [cat_data]

            entries = self._consolidate_category_entries(cat_num, entries)
            category_facts_added = 0

            # If Category 1 (patient_details) already has a preserved entry, do not add a second entry
            if cat_num == 1 and preserved_entry_ids_by_cat[cat_num]:
                existing_entry_id = sorted(preserved_entry_ids_by_cat[cat_num])[0]
                if entries:
                    facts = self._flatten_entry(
                        entry=entries[0],
                        category_number=cat_num,
                        category_key=cat_key,
                        entry_id=existing_entry_id,
                        patient_id=patient_id,
                        snapshot_id=snapshot_id
                    )
                    for fact in facts:
                        if fact.field_name in preserved_fields_by_cat[cat_num]:
                            continue
                        self.db.add(fact)
                        preserved_fields_by_cat[cat_num].add(fact.field_name)
                        fact_count += 1
                        category_facts_added += 1
                continue

            next_idx = 0
            for entry in entries:
                # Check if this entry duplicates an already-preserved entry by primary value
                is_dup_of_preserved = False
                for pk in primary_keys:
                    val = entry.get(pk)
                    if val not in (None, "", []):
                        norm_val = " ".join(str(val).strip().lower().split())
                        if (pk, norm_val) in preserved_primary_vals_by_cat[cat_num]:
                            is_dup_of_preserved = True
                        break
                if is_dup_of_preserved:
                    continue

                while f"{cat_key}_{next_idx}" in preserved_entry_ids_by_cat[cat_num]:
                    next_idx += 1
                entry_id = f"{cat_key}_{next_idx}"
                next_idx += 1

                facts = self._flatten_entry(
                    entry=entry,
                    category_number=cat_num,
                    category_key=cat_key,
                    entry_id=entry_id,
                    patient_id=patient_id,
                    snapshot_id=snapshot_id
                )
                for fact in facts:
                    norm_val = " ".join((fact.value or "").strip().lower().split())
                    sig = (
                        fact.category_number,
                        fact.field_name,
                        norm_val
                    )
                    if sig in preserved_signatures:
                        continue
                    self.db.add(fact)
                    fact_count += 1
                    category_facts_added += 1

            # If category had no documented facts and no preserved facts, record explicit not_documented row
            has_preserved_in_cat = any(
                s[0] == cat_num for s in preserved_signatures)
            if category_facts_added == 0 and not has_preserved_in_cat:
                not_doc_fact = ConsolidatedFact(
                    id=uuid.uuid4(),
                    patient_id=patient_id,
                    category_number=cat_num,
                    category_key=cat_key,
                    entry_id=f"{cat_key}_none",
                    field_name="status",
                    value=None,
                    value_json={"snapshot_id": str(snapshot_id), "unit": None},
                    availability="not_documented",
                    assertion=None,
                    temporality=None,
                    experiencer=None,
                    event_time=None,
                    original_time_expression=None,
                    evidence=[],
                    origin="extraction",
                    review_status="unreviewed"
                )
                self.db.add(not_doc_fact)

        self.db.commit()
        return fact_count

    def _consolidate_category_entries(
        self,
        cat_num: int,
        entries: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Consolidate duplicate entries extracted across multiple documents for the same patient.
        - Category 1 (patient_details): merge all documented entries into a single patient record.
        - Other categories: merge entries that share the same primary clinical value and context.
        """
        if not entries or len(entries) <= 1:
            return entries

        documented = [
            e for e in entries
            if isinstance(e, dict) and e.get("availability", "documented") == "documented"
        ]
        if not documented:
            return entries[:1]

        # Category 1: Patient details should always be consolidated into 1 entry per patient
        if cat_num == 1:
            merged = dict(documented[0])
            combined_ev = list(merged.get("evidence") or [])
            for extra in documented[1:]:
                for k, v in extra.items():
                    if k == "evidence":
                        for ev in (v or []):
                            if ev not in combined_ev:
                                combined_ev.append(ev)
                    elif (merged.get(k) is None or merged.get(k) == "") and v not in (None, "", []):
                        merged[k] = v
            merged["evidence"] = combined_ev
            return [merged]

        # Other categories: deduplicate by primary subfield value + context
        primary_keys = [
            "diagnosis", "name", "test_name", "measurement_name", "substance",
            "symptom", "condition", "procedure", "main_complaint", "action",
            "instruction", "event", "finding", "category"
        ]
        deduped: List[Dict[str, Any]] = []
        seen_map: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}

        for entry in documented:
            primary_val = ""
            for pk in primary_keys:
                val = entry.get(pk)
                if val not in (None, "", []):
                    primary_val = f"{pk}:{str(val).strip().lower()}"
                    break
            if not primary_val:
                deduped.append(entry)
                continue

            key = (
                primary_val,
                str(entry.get("assertion") or "present").lower(),
                "" if cat_num == 11 else str(entry.get("temporality") or "current").lower(),
                str(entry.get("experiencer") or "patient").lower(),
            )
            if key not in seen_map:
                copy_entry = dict(entry)
                copy_entry["evidence"] = list(entry.get("evidence") or [])
                seen_map[key] = copy_entry
                deduped.append(copy_entry)
            else:
                target = seen_map[key]
                if cat_num == 11 and str(entry.get("temporality") or "current").lower() == "current":
                    target["temporality"] = "current"
                for k, v in entry.items():
                    if k == "evidence":
                        for ev in (v or []):
                            if ev not in target["evidence"]:
                                target["evidence"].append(ev)
                    elif (target.get(k) is None or target.get(k) == "") and v not in (None, "", []):
                        target[k] = v

        return deduped

    def _flatten_entry(
        self,
        entry: Dict[str, Any],
        category_number: int,
        category_key: str,
        entry_id: str,
        patient_id: uuid.UUID,
        snapshot_id: uuid.UUID
    ) -> List[ConsolidatedFact]:
        """Flatten a validated clinical entry into individual ConsolidatedFact rows."""
        facts: List[ConsolidatedFact] = []
        if not isinstance(entry, dict):
            return facts

        availability = entry.get("availability", "documented")
        if availability in ("not_documented", "unreadable"):
            return facts

        unit_val = entry.get("unit")

        for field_name, value in entry.items():
            if field_name in METADATA_KEYS:
                continue

            if value is None or value == "" or value == []:
                continue

            if isinstance(value, list):
                str_value = ", ".join(str(v) for v in value if v is not None)
            elif isinstance(value, dict):
                str_value = ", ".join(
                    f"{k}: {v}" for k, v in value.items() if v is not None)
            else:
                str_value = str(value).strip()

            if not str_value:
                continue

            # Attach unit to measurement value subfields
            field_unit = unit_val if field_name in (
                "value", "result", "dose") else None

            fact = ConsolidatedFact(
                id=uuid.uuid4(),
                patient_id=patient_id,
                category_number=category_number,
                category_key=category_key,
                entry_id=entry_id,
                field_name=field_name,
                value=str_value,
                value_json={
                    "snapshot_id": str(snapshot_id),
                    "unit": field_unit,
                    "raw": value if isinstance(value, (dict, list)) else None
                },
                availability=availability,
                assertion=entry.get("assertion", "present"),
                temporality=entry.get("temporality", "current"),
                experiencer=entry.get("experiencer", "patient"),
                event_time=entry.get("event_time"),
                original_time_expression=entry.get("original_time_expression"),
                evidence=entry.get("evidence") or [],
                origin=entry.get("origin", "extraction"),
                review_status="unreviewed"
            )
            facts.append(fact)

        return facts

    async def regenerate_patient_summary(self, patient_id: uuid.UUID) -> PatientSummary:
        """Regenerate patient summary from current consolidated facts without rerunning extraction."""
        latest_job = (
            self.db.query(ProcessingJob)
            .filter(ProcessingJob.patient_id == patient_id)
            .order_by(ProcessingJob.created_at.desc())
            .first()
        )
        snapshot_id = None
        if latest_job and latest_job.snapshot:
            snapshot_id = latest_job.snapshot.id
        return await self._generate_summary(patient_id, snapshot_id)

    async def _generate_summary(
        self,
        patient_id: uuid.UUID,
        snapshot_id: Optional[uuid.UUID] = None
    ) -> PatientSummary:
        """Generate grounded patient summary and store it."""
        facts = self.db.query(ConsolidatedFact).filter(
            ConsolidatedFact.patient_id == patient_id,
            ConsolidatedFact.review_status != "rejected",
            ConsolidatedFact.availability == "documented"
        ).order_by(ConsolidatedFact.category_number, ConsolidatedFact.entry_id).all()

        fact_dicts = []
        for fact in facts:
            if not fact.value:
                continue
            unit = None
            if isinstance(fact.value_json, dict):
                unit = fact.value_json.get("unit")
            fact_dicts.append({
                "id": str(fact.id),
                "category_key": fact.category_key,
                "field_name": fact.field_name,
                "value": fact.value,
                "unit": unit,
                "availability": fact.availability,
                "assertion": fact.assertion,
                "temporality": fact.temporality,
                "experiencer": fact.experiencer,
                "event_time": fact.event_time,
                "review_status": fact.review_status
            })

        patient = self.db.query(Patient).filter(
            Patient.id == patient_id).first()
        patient_name = patient.display_name if patient else "Patient"

        result = await patient_summary_service.generate_summary(fact_dicts, patient_name)

        # Mark older summaries stale
        self.db.query(PatientSummary).filter(
            PatientSummary.patient_id == patient_id
        ).update({"is_stale": True})

        summary = PatientSummary(
            id=uuid.uuid4(),
            patient_id=patient_id,
            summary_text=result["summary_text"],
            sentences=result["sentences"],
            status="generated" if not result.get(
                "validation_errors") else "failed",
            is_stale=False,
            validation_errors=result.get("validation_errors"),
            fact_revision=len(fact_dicts),
            snapshot_id=snapshot_id
        )

        self.db.add(summary)
        self.db.commit()
        self.db.refresh(summary)

        logger.info(f"Generated summary {summary.id} for patient {patient_id}")
        return summary

    async def _lookup_icd_codes(self, patient_id: uuid.UUID) -> str:
        """
        Lookup WHO ICD-11 MMS codes for eligible patient diagnoses.
        Excludes negated diagnoses, family history diagnoses, and rejected facts.
        Never auto-accepts candidates (always sets mapping_status='suggested').
        """
        all_diag_facts = self.db.query(ConsolidatedFact).filter(
            ConsolidatedFact.patient_id == patient_id,
            ConsolidatedFact.category_number == 11,
            ConsolidatedFact.field_name == "diagnosis",
            ConsolidatedFact.availability == "documented",
            ConsolidatedFact.review_status != "rejected"
        ).all()

        eligible_facts = [
            f for f in all_diag_facts
            if f.value
            and (f.assertion or "present") != "negated"
            and (f.experiencer or "patient") == "patient"
        ]

        if not eligible_facts:
            logger.info(
                f"No eligible patient diagnoses to lookup for patient {patient_id}")
            return "not_applicable"

        if not who_icd_service.is_configured():
            logger.warning(
                "WHO ICD not configured; recording lookup_failed decisions for visibility")
            for fact in eligible_facts:
                existing_dec = self.db.query(ICDDecision).filter(
                    ICDDecision.fact_id == fact.id).first()
                if not existing_dec:
                    self.db.add(ICDDecision(
                        id=uuid.uuid4(),
                        fact_id=fact.id,
                        mapping_status="lookup_failed",
                        reviewer_note="WHO ICD-11 credentials not configured in .env",
                        icd_release=settings.who_icd_release,
                        icd_language=settings.who_icd_language
                    ))
            self.db.commit()
            return "skipped_unconfigured"

        diagnosis_texts = list({f.value for f in eligible_facts if f.value})

        try:
            results = await who_icd_service.lookup_multiple(diagnosis_texts, max_results_per_diagnosis=5)

            for fact in eligible_facts:
                existing_decisions = self.db.query(ICDDecision).filter(
                    ICDDecision.fact_id == fact.id
                ).all()
                # Preserve accepted decision if it does not need review
                if any(d.mapping_status == "accepted" and not d.needs_review for d in existing_decisions):
                    continue

                # Clear stale non-accepted decisions before inserting fresh suggestions
                for d in existing_decisions:
                    self.db.delete(d)
                self.db.flush()

                candidates_list = results.get(fact.value, [])
                norm_query = who_icd_service.sanitize_diagnosis_query(
                    fact.value).lower()

                if not candidates_list:
                    self.db.add(ICDDecision(
                        id=uuid.uuid4(),
                        fact_id=fact.id,
                        mapping_status="unresolved",
                        icd_release=settings.who_icd_release,
                        icd_language=settings.who_icd_language
                    ))
                    continue

                first_candidate_obj = None
                for cand_data in candidates_list:
                    candidate = self.db.query(ICDCandidate).filter(
                        ICDCandidate.mms_code == cand_data["mms_code"],
                        ICDCandidate.normalized_query == norm_query,
                        ICDCandidate.icd_release == settings.who_icd_release
                    ).first()

                    if not candidate:
                        candidate = ICDCandidate(
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
                        self.db.add(candidate)
                        self.db.flush()

                    if first_candidate_obj is None:
                        first_candidate_obj = candidate

                # Create a suggested decision pointing to top candidate (never auto-accepted)
                if first_candidate_obj:
                    self.db.add(ICDDecision(
                        id=uuid.uuid4(),
                        fact_id=fact.id,
                        candidate_id=first_candidate_obj.id,
                        mapping_status="suggested",
                        needs_review=False,
                        icd_release=settings.who_icd_release,
                        icd_language=settings.who_icd_language,
                        suggested_at=datetime.utcnow()
                    ))

            self.db.commit()
            logger.info(
                f"Completed ICD lookup for {len(eligible_facts)} diagnoses")
            return "completed"

        except WHOICDError as e:
            logger.error(f"ICD lookup failed: {e}")
            for fact in eligible_facts:
                existing_dec = self.db.query(ICDDecision).filter(
                    ICDDecision.fact_id == fact.id).first()
                if not existing_dec:
                    self.db.add(ICDDecision(
                        id=uuid.uuid4(),
                        fact_id=fact.id,
                        mapping_status="lookup_failed",
                        reviewer_note=str(e),
                        icd_release=settings.who_icd_release,
                        icd_language=settings.who_icd_language
                    ))
            self.db.commit()
            return "failed"


def create_processing_service(db: Session) -> ProcessingService:
    """Create processing service instance."""
    return ProcessingService(db)
