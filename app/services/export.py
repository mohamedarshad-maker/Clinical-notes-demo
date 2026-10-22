"""CSV export service with proper encoding and formula injection protection."""
import csv
import io
import logging
import re
import uuid
from typing import List, Dict, Any, Optional, Union
from sqlalchemy.orm import Session, joinedload
from app.models import (
    Patient, ConsolidatedFact, PatientSummary,
    ICDDecision, ProcessingJob, ExtractionSnapshot
)
from app.schemas.clinical import CATEGORY_DEFINITIONS

logger = logging.getLogger(__name__)

NUMERIC_VALUE_RE = re.compile(
    r"^[+-]?(\d+(\.\d+)?|\.\d+)(\s*[a-zA-Z%/°µ]+.*)?$")


class ExportService:
    """Handles CSV export of clinical data matching Section 11 contracts."""

    def __init__(self, db: Session):
        """Initialize with database session."""
        self.db = db

    def _to_uuid(self, patient_id: Union[str, uuid.UUID]) -> uuid.UUID:
        if isinstance(patient_id, uuid.UUID):
            return patient_id
        return uuid.UUID(str(patient_id))

    def _get_latest_snapshot_id(self, patient_uuid: uuid.UUID) -> str:
        latest_job = (
            self.db.query(ProcessingJob)
            .filter(ProcessingJob.patient_id == patient_uuid)
            .order_by(ProcessingJob.created_at.desc())
            .first()
        )
        if latest_job:
            snap = (
                self.db.query(ExtractionSnapshot)
                .filter(ExtractionSnapshot.job_id == latest_job.id)
                .first()
            )
            if snap:
                return str(snap.id)
        return ""

    def export_clinical_facts(
        self,
        patient_id: Union[str, uuid.UUID],
        include_unaccepted_icd: bool = False,
        include_not_documented: bool = True,
        include_rejected: bool = False
    ) -> str:
        """
        Export patient clinical facts to CSV matching the 25-column Section 11.1 contract.
        """
        patient_uuid = self._to_uuid(patient_id)
        patient = self.db.query(Patient).filter(
            Patient.id == patient_uuid).first()
        if not patient:
            raise ValueError(f"Patient {patient_id} not found")

        default_snapshot_id = self._get_latest_snapshot_id(patient_uuid)

        facts = self.db.query(ConsolidatedFact).filter(
            ConsolidatedFact.patient_id == patient_uuid
        ).order_by(
            ConsolidatedFact.category_number,
            ConsolidatedFact.entry_id,
            ConsolidatedFact.field_name
        ).all()

        diag_fact_ids = [
            f.id for f in facts
            if f.category_number == 11 and f.field_name == "diagnosis"
        ]
        decisions_by_fact: Dict[uuid.UUID, List[ICDDecision]] = {}
        if diag_fact_ids:
            all_decs = (
                self.db.query(ICDDecision)
                .options(joinedload(ICDDecision.candidate))
                .filter(ICDDecision.fact_id.in_(diag_fact_ids))
                .all()
            )
            for dec in all_decs:
                decisions_by_fact.setdefault(dec.fact_id, []).append(dec)

        rows: List[Dict[str, str]] = []
        documented_categories = set()

        for fact in facts:
            if not include_rejected and fact.review_status == "rejected":
                continue

            if fact.availability == "not_documented":
                if not include_not_documented:
                    continue
            else:
                documented_categories.add(fact.category_number)

            # Extract unit & snapshot_id from value_json if present
            unit_str = ""
            fact_snapshot_id = default_snapshot_id
            if isinstance(fact.value_json, dict):
                unit_str = str(fact.value_json.get("unit") or "")
                fact_snapshot_id = str(fact.value_json.get(
                    "snapshot_id") or default_snapshot_id)

            icd_rows = []
            if fact.category_number == 11 and fact.field_name == "diagnosis":
                decisions = decisions_by_fact.get(fact.id, [])

                for decision in decisions:
                    # Default export includes only reviewer-accepted codes that are not stale (needs_review=False)
                    if decision.mapping_status == "accepted" and not decision.needs_review:
                        icd_rows.append(self._get_icd_row_data(decision))
                    elif include_unaccepted_icd:
                        icd_rows.append(self._get_icd_row_data(decision))

            base_row = {
                "patient_id": str(patient.id),
                "patient_display_name": self._sanitize_value(patient.display_name),
                "snapshot_id": fact_snapshot_id,
                "category": fact.category_key,
                "fact_id": str(fact.id),
                "subfield": fact.field_name,
                "value": self._sanitize_value(fact.value or ""),
                "unit": self._sanitize_value(unit_str),
                "availability": fact.availability,
                "assertion": fact.assertion or "",
                "temporality": fact.temporality or "",
                "experiencer": fact.experiencer or "",
                "event_time": self._sanitize_value(fact.event_time or ""),
                "origin": fact.origin,
                "review_status": fact.review_status,
                "source_document": "",
                "source_page": "",
                "source_block": "",
                "evidence_quote": "",
                "icd_system": "",
                "icd_code": "",
                "icd_description": "",
                "icd_release": "",
                "icd_mapping_status": "",
                "icd_needs_review": ""
            }

            if fact.evidence and len(fact.evidence) > 0:
                first_evidence = fact.evidence[0]
                base_row["source_document"] = str(
                    first_evidence.get("document_id") or "")
                base_row["source_page"] = str(
                    first_evidence.get("page_number") or "")
                base_row["source_block"] = str(
                    first_evidence.get("block_id") or "")
                base_row["evidence_quote"] = self._sanitize_value(
                    first_evidence.get("quote") or "")

            if icd_rows:
                first_icd = icd_rows[0]
                row_with_icd = base_row.copy()
                row_with_icd.update(first_icd)
                rows.append(row_with_icd)

                for icd_data in icd_rows[1:]:
                    extra_row = base_row.copy()
                    extra_row.update(icd_data)
                    rows.append(extra_row)
            else:
                rows.append(base_row)

        return self._generate_csv(rows, self._get_facts_csv_headers())

    def _get_icd_row_data(self, decision: ICDDecision) -> Dict[str, str]:
        """Get ICD data for a CSV row."""
        icd_data = {
            "icd_system": "ICD-11 MMS",
            "icd_code": "",
            "icd_description": "",
            "icd_release": decision.icd_release or "",
            "icd_mapping_status": decision.mapping_status or "",
            "icd_needs_review": "yes" if decision.needs_review else "no"
        }

        if decision.candidate:
            icd_data["icd_code"] = decision.candidate.mms_code or ""
            icd_data["icd_description"] = self._sanitize_value(
                decision.candidate.mms_title or ""
            )

        return icd_data

    def export_patient_summary(self, patient_id: Union[str, uuid.UUID]) -> str:
        """
        Export patient summary to CSV.
        """
        patient_uuid = self._to_uuid(patient_id)
        patient = self.db.query(Patient).filter(
            Patient.id == patient_uuid).first()
        if not patient:
            raise ValueError(f"Patient {patient_id} not found")

        summary = self.db.query(PatientSummary).filter(
            PatientSummary.patient_id == patient_uuid
        ).order_by(PatientSummary.generated_at.desc()).first()

        if not summary:
            rows = [{
                "patient_id": str(patient.id),
                "patient_display_name": self._sanitize_value(patient.display_name),
                "summary_id": "",
                "snapshot_id": "",
                "summary_text": "No summary available",
                "status": "not_generated",
                "is_stale": "",
                "generated_at": ""
            }]
        else:
            rows = [{
                "patient_id": str(patient.id),
                "patient_display_name": self._sanitize_value(patient.display_name),
                "summary_id": str(summary.id),
                "snapshot_id": str(summary.snapshot_id) if summary.snapshot_id else "",
                "summary_text": self._sanitize_value(summary.summary_text),
                "status": summary.status,
                "is_stale": "yes" if summary.is_stale else "no",
                "generated_at": summary.generated_at.isoformat() if summary.generated_at else ""
            }]

        headers = [
            "patient_id", "patient_display_name", "summary_id", "snapshot_id",
            "summary_text", "status", "is_stale", "generated_at"
        ]

        return self._generate_csv(rows, headers)

    def _get_facts_csv_headers(self) -> List[str]:
        """Exact 25-column CSV header contract from Section 11.1."""
        return [
            "patient_id",
            "patient_display_name",
            "snapshot_id",
            "category",
            "fact_id",
            "subfield",
            "value",
            "unit",
            "availability",
            "assertion",
            "temporality",
            "experiencer",
            "event_time",
            "origin",
            "review_status",
            "source_document",
            "source_page",
            "source_block",
            "evidence_quote",
            "icd_system",
            "icd_code",
            "icd_description",
            "icd_release",
            "icd_mapping_status",
            "icd_needs_review"
        ]

    def _generate_csv(self, rows: List[Dict], headers: List[str]) -> str:
        """Generate RFC 4180 CSV string from rows."""
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=headers,
            extrasaction="ignore",
            lineterminator="\r\n"
        )
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue()

    def _sanitize_value(self, value: Any) -> str:
        """
        Sanitize value for CSV to prevent formula injection while preserving
        legitimate negative/positive numeric clinical measurements (e.g. -1.5, -2.0 D).
        """
        if value is None:
            return ""

        str_value = str(value)
        if not str_value:
            return ""

        first_char = str_value[0]
        if first_char in ("-", "+"):
            # Preserve genuine negative/positive numeric measurements
            if NUMERIC_VALUE_RE.match(str_value.strip()):
                return str_value
            return "'" + str_value

        if first_char in ("=", "@", "\t", "\r", "|"):
            return "'" + str_value

        return str_value


def create_export_service(db: Session) -> ExportService:
    """Create export service instance."""
    return ExportService(db)
