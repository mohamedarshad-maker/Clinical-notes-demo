"""Comprehensive test suite for Clinical Notes Extraction PoC."""
import base64
import csv
import io
import uuid
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from main import app
from app.config import settings
from app.database import Base, get_db
from app.models import (
    Patient,
    Document,
    DocumentPage,
    ProcessingJob,
    ExtractionSnapshot,
    ConsolidatedFact,
    ICDCandidate,
    ICDDecision,
    PatientSummary,
    ReviewRevision,
)
from app.schemas.clinical import (
    GroupAOutput,
    GroupBOutput,
    GroupCOutput,
    DiagnosisEntry,
    ClinicalEvidence,
    CATEGORY_DEFINITIONS,
)
from app.services.document_parser import document_parser
from app.services.who_icd import WHOICDService
from app.services.extraction import ExtractionService
from app.services.export import ExportService

# Test database
SQLALCHEMY_DATABASE_URL = "sqlite:///./test_clinical_notes.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    """Override database for tests."""
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture
def db_session():
    """Database session fixture with clean schema per test."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db_session, monkeypatch):
    """Test client fixture with demo auth disabled by default unless tested explicitly."""
    monkeypatch.setattr(settings, "demo_auth_username", None)
    monkeypatch.setattr(settings, "demo_auth_password", None)
    monkeypatch.setattr(settings, "demo_username", None)
    monkeypatch.setattr(settings, "demo_password", None)
    with TestClient(app) as c:
        yield c


def test_health_check(client):
    """Test health endpoint returns readiness statuses."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "llm_provider" in data
    assert "llm_configured" in data
    assert "who_icd_configured" in data
    assert "storage_configured" in data
    assert "ocr_configured" in data


def test_create_list_delete_patient(client):
    """Test patient creation, listing, detail profile, and deletion."""
    response = client.post(
        "/patients/",
        data={"display_name": "Jane Doe"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    patient_id = location.split("/patients/")[-1]

    # List patients
    list_resp = client.get("/patients/")
    assert list_resp.status_code == 200
    assert "Jane Doe" in list_resp.text

    # Patient detail HTML
    detail_resp = client.get(f"/patients/{patient_id}")
    assert detail_resp.status_code == 200
    assert "Jane Doe" in detail_resp.text

    # Patient profile JSON API
    profile_resp = client.get(f"/api/patients/{patient_id}/profile")
    assert profile_resp.status_code == 200
    profile_data = profile_resp.json()
    assert profile_data["patient"]["display_name"] == "Jane Doe"
    assert len(profile_data["categories"]) == 19

    # Delete patient
    del_resp = client.delete(f"/patients/{patient_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["success"] is True


def test_strict_schema_forbids_extra_fields():
    """Verify Pydantic schemas enforce extra='forbid' and have all 19 categories."""
    assert len(CATEGORY_DEFINITIONS) == 19

    with pytest.raises(ValidationError):
        DiagnosisEntry(
            diagnosis="Type 2 diabetes mellitus",
            evidence=[
                ClinicalEvidence(
                    document_id="doc1",
                    page_number=1,
                    quote="Type 2 diabetes mellitus",
                )
            ],
            unexpected_extra_field="should fail",
        )


@pytest.mark.asyncio
async def test_document_parser_text():
    """Test text parsing and block extraction."""
    text_content = b"Chief Complaint: Chest pain for 2 days.\n\nVital Signs: BP 140/90 mmHg."
    results = await document_parser.parse_document(text_content, "text/plain")
    assert len(results) == 1
    assert results[0].page_number == 1
    assert results[0].extraction_method == "direct"
    assert results[0].is_readable is True
    assert len(results[0].blocks) == 2
    assert results[0].blocks[0]["block_id"] == "p1_b0"


def test_upload_validation_and_paste_flow(client):
    """Test file extension/magic-byte validation, text upload, and paste note endpoints."""
    # Create patient
    resp = client.post("/patients/", data={"display_name": "Upload Test Patient"}, follow_redirects=False)
    patient_id = resp.headers["location"].split("/patients/")[-1]

    # 1. Invalid spoofed PDF (text bytes with .pdf extension) should fail magic byte check
    spoof_resp = client.post(
        f"/documents/upload/{patient_id}",
        files=[("files", ("fake.pdf", b"not a real pdf", "application/pdf"))],
    )
    assert spoof_resp.status_code == 200
    res_item = spoof_resp.json()["results"][0]
    assert res_item["success"] is False
    assert "header" in res_item["error"].lower()

    # 2. Valid text file upload
    txt_resp = client.post(
        f"/documents/upload/{patient_id}",
        files=[
            (
                "files",
                (
                    "clinical_note.txt",
                    b"Patient has Essential hypertension and takes Lisinopril 10 mg daily.",
                    "text/plain",
                ),
            )
        ],
    )
    assert txt_resp.status_code == 200
    txt_item = txt_resp.json()["results"][0]
    assert txt_item["success"] is True
    doc_id = txt_item["document_id"]

    # 3. Inspect pages endpoint
    pages_resp = client.get(f"/documents/{doc_id}/pages")
    assert pages_resp.status_code == 200
    pages_data = pages_resp.json()
    assert len(pages_data["pages"]) == 1
    assert "Essential hypertension" in pages_data["pages"][0]["raw_text"]

    # 4. Raw download endpoint
    dl_resp = client.get(f"/documents/{doc_id}/download")
    assert dl_resp.status_code == 200
    assert b"Essential hypertension" in dl_resp.content

    # 5. Paste clinical note
    paste_resp = client.post(
        f"/documents/paste/{patient_id}",
        data={"text": "Allergies: Penicillin causes rash and hives.", "title": "Allergy Note"},
    )
    assert paste_resp.status_code == 200
    assert paste_resp.json()["success"] is True


@pytest.mark.asyncio
async def test_evidence_verification_and_deduplication(db_session):
    """Test that ExtractionService verifies quotes against snapshot text and preserves conflicts."""
    patient = Patient(id=uuid.uuid4(), display_name="Evidence Patient")
    db_session.add(patient)
    db_session.commit()

    doc = Document(
        id=uuid.uuid4(),
        patient_id=patient.id,
        original_filename="note.txt",
        storage_bucket="clinical-documents",
        storage_path="patients/test/note.txt",
        content_type="text/plain",
        file_size_bytes=120,
        content_hash="abc123",
        source_type="upload",
        upload_status="completed",
        parsing_status="completed",
    )
    db_session.add(doc)
    db_session.commit()

    note_text = "Patient has Type 2 diabetes mellitus. Denies chest pain. Family history of colon cancer."
    page = DocumentPage(
        id=uuid.uuid4(),
        document_id=doc.id,
        page_number=1,
        raw_text=note_text,
        normalized_text=note_text,
        extraction_method="direct",
        is_readable=True,
        blocks=[
            {
                "block_id": "p1_b0",
                "text": note_text,
                "order": 0,
            }
        ],
    )
    job = ProcessingJob(
        id=uuid.uuid4(),
        patient_id=patient.id,
        status="running",
        stages={},
    )
    db_session.add_all([page, job])
    db_session.commit()

    extraction_svc = ExtractionService(db_session)
    snapshot = await extraction_svc.create_snapshot(
        patient_id=patient.id,
        document_ids=[doc.id],
        job_id=job.id,
    )
    assert "[BLOCK: p1_b0]" in snapshot.combined_text

    # 1. Valid quote passes verification (Diagnoses is Category 11 -> GroupBOutput.diagnoses_assessment)
    valid_group_b = GroupBOutput(
        diagnoses_assessment=[
            DiagnosisEntry(
                diagnosis="Type 2 diabetes mellitus",
                assertion="present",
                experiencer="patient",
                evidence=[
                    ClinicalEvidence(
                        document_id=str(doc.id),
                        page_number=1,
                        block_id="p1_b0",
                        quote="Type 2 diabetes mellitus",
                    )
                ],
            )
        ]
    ).model_dump()
    extraction_svc._validate_and_normalize_group_evidence(valid_group_b, snapshot)

    # 2. Fabricated quote raises ValueError
    fabricated_group_b = GroupBOutput(
        diagnoses_assessment=[
            DiagnosisEntry(
                diagnosis="Hallucinated asthma",
                assertion="present",
                experiencer="patient",
                evidence=[
                    ClinicalEvidence(
                        document_id=str(doc.id),
                        page_number=1,
                        block_id="p1_b0",
                        quote="Patient has severe refractory bronchial asthma",
                    )
                ],
            )
        ]
    ).model_dump()
    with pytest.raises(ValueError, match="not found"):
        extraction_svc._validate_and_normalize_group_evidence(fabricated_group_b, snapshot)

    # 3. Deduplication test: identical entries merge, conflicting assertions stay separate
    dup_list = [
        {
            "diagnosis": "Hypertension",
            "assertion": "present",
            "temporality": "current",
            "experiencer": "patient",
            "event_time": "2025-01-01",
            "evidence": [{"document_id": str(doc.id), "page_number": 1, "quote": "Hypertension"}],
        },
        {
            "diagnosis": "hypertension",
            "assertion": "present",
            "temporality": "current",
            "experiencer": "patient",
            "event_time": "2025-01-01",
            "evidence": [{"document_id": str(doc.id), "page_number": 1, "quote": "Hypertension"}],
        },
        {
            "diagnosis": "Hypertension",
            "assertion": "negated",  # Conflicting assertion - must NOT be merged!
            "temporality": "current",
            "experiencer": "patient",
            "event_time": "2025-01-02",
            "evidence": [{"document_id": str(doc.id), "page_number": 1, "quote": "No hypertension"}],
        },
    ]
    deduped = extraction_svc._deduplicate_entries(dup_list)
    assert len(deduped) == 2


@pytest.mark.asyncio
async def test_who_icd_html_stripping_and_chapter_x_filtering(monkeypatch):
    """Verify WHO ICD-11 service strips <em> tags, excludes Chapter X extension codes, and uses /release/11/."""
    svc = WHOICDService()
    assert svc._strip_html("<em class='found'>Type</em> 2 <em class='found'>diabetes</em> mellitus") == "Type 2 diabetes mellitus"
    assert "john@example.com" not in svc.sanitize_diagnosis_query("Hypertension john@example.com 987654321")

    monkeypatch.setattr(settings, "who_icd_client_id", "test-client-id")
    monkeypatch.setattr(settings, "who_icd_client_secret", "test-client-secret")

    async def fake_token():
        return "mock-token"

    monkeypatch.setattr(svc, "get_access_token", fake_token)

    captured_urls = []

    class DummyResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "destinationEntities": [
                    {
                        "id": "http://id.who.int/icd/entity/1697306310",
                        "theCode": "5A11",
                        "title": "<em class='found'>Type</em> 2 <em class='found'>diabetes</em> mellitus",
                        "chapter": "05",
                    },
                    {
                        "id": "http://id.who.int/icd/entity/999999",
                        "theCode": "XK9J",
                        "title": "<em class='found'>Extension</em> code severity",
                        "chapter": "X",
                    },
                    {
                        "id": "http://id.who.int/icd/entity/888888",
                        "theCode": "",
                        "title": "Grouping without code",
                        "chapter": "05",
                    },
                ]
            }

    import httpx

    async def fake_get(self_client, url, params=None, headers=None):
        captured_urls.append(url)
        return DummyResponse()

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    candidates = await svc.search_diagnosis("Type 2 diabetes mellitus")
    assert len(captured_urls) == 1
    assert "/release/11/" in captured_urls[0]
    assert len(candidates) == 1
    assert candidates[0]["mms_code"] == "5A11"
    assert candidates[0]["mms_title"] == "Type 2 diabetes mellitus"
    assert "<em" not in candidates[0]["mms_title"]
    assert candidates[0]["who_source_link"].startswith("https://")


def test_fact_review_staleness_and_icd_invalidation(client, db_session):
    """Test that editing a diagnosis fact creates a ReviewRevision, marks summary stale, and flags accepted ICD as needs_review."""
    patient = Patient(id=uuid.uuid4(), display_name="Review Patient")
    db_session.add(patient)
    db_session.commit()

    fact = ConsolidatedFact(
        id=uuid.uuid4(),
        patient_id=patient.id,
        category_number=11,
        category_key="diagnoses",
        entry_id="11_0",
        field_name="diagnosis",
        value="Type 2 diabetes mellitus",
        value_json={"unit": None, "snapshot_id": "snap-1", "raw": "Type 2 diabetes mellitus"},
        availability="documented",
        assertion="present",
        temporality="current",
        experiencer="patient",
        origin="extraction",
        review_status="unreviewed",
        evidence=[{"document_id": "doc-1", "page_number": 1, "block_id": "p1_b0", "quote": "Type 2 diabetes mellitus"}],
    )
    db_session.add(fact)
    db_session.commit()

    candidate = ICDCandidate(
        id=uuid.uuid4(),
        diagnosis_text="Type 2 diabetes mellitus",
        normalized_query="type 2 diabetes mellitus",
        mms_code="5A11",
        mms_title="Type 2 diabetes mellitus",
        foundation_uri="https://id.who.int/icd/entity/1697306310",
        who_source_link="https://id.who.int/icd/entity/1697306310",
        icd_release="2026-01",
        icd_language="en",
        search_rank=1,
    )
    db_session.add(candidate)
    db_session.flush()

    decision = ICDDecision(
        id=uuid.uuid4(),
        fact_id=fact.id,
        candidate_id=candidate.id,
        mapping_status="accepted",
        needs_review=False,
        icd_release="2026-01",
        icd_language="en",
    )
    summary = PatientSummary(
        id=uuid.uuid4(),
        patient_id=patient.id,
        summary_text="Patient has Type 2 diabetes mellitus.",
        sentences=[{"sentence": "Patient has Type 2 diabetes mellitus.", "fact_ids": [str(fact.id)]}],
        status="completed",
        is_stale=False,
        fact_revision=1,
    )
    db_session.add_all([decision, summary])
    db_session.commit()

    # Edit diagnosis text via PATCH /facts/{fact_id}
    patch_resp = client.patch(
        f"/facts/{fact.id}",
        json={
            "value": "Type 1 diabetes mellitus",
            "review_status": "corrected",
            "reviewer_note": "Corrected diabetes type",
        },
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["success"] is True

    db_session.expire_all()
    updated_fact = db_session.query(ConsolidatedFact).filter(ConsolidatedFact.id == fact.id).first()
    updated_decision = db_session.query(ICDDecision).filter(ICDDecision.fact_id == fact.id).first()
    updated_summary = db_session.query(PatientSummary).filter(PatientSummary.patient_id == patient.id).first()
    revisions = db_session.query(ReviewRevision).filter(ReviewRevision.fact_id == fact.id).all()

    assert updated_fact.value == "Type 1 diabetes mellitus"
    assert updated_fact.review_status == "corrected"
    assert updated_fact.origin == "reviewer_correction"
    assert updated_decision.needs_review is True
    assert updated_summary.is_stale is True
    assert len(revisions) == 1


def test_csv_export_25_columns_and_formula_safety(client, db_session):
    """Test 25-column CSV export contract, negative number preservation, formula escaping, and ICD acceptance rules."""
    patient = Patient(id=uuid.uuid4(), display_name="Export Patient")
    db_session.add(patient)
    db_session.commit()

    fact = ConsolidatedFact(
        id=uuid.uuid4(),
        patient_id=patient.id,
        category_number=11,
        category_key="diagnoses",
        entry_id="11_0",
        field_name="diagnosis",
        value="Essential hypertension",
        value_json={"unit": None, "snapshot_id": "snap-123", "raw": "Essential hypertension"},
        availability="documented",
        assertion="present",
        temporality="current",
        experiencer="patient",
        origin="extraction",
        review_status="confirmed",
        evidence=[{"document_id": "doc-1", "page_number": 2, "block_id": "p2_b1", "quote": "Essential hypertension"}],
    )
    db_session.add(fact)
    db_session.commit()

    candidate = ICDCandidate(
        id=uuid.uuid4(),
        diagnosis_text="Essential hypertension",
        normalized_query="essential hypertension",
        mms_code="BA00",
        mms_title="Essential hypertension",
        foundation_uri="https://id.who.int/icd/entity/2",
        who_source_link="https://id.who.int/icd/entity/2",
        icd_release="2026-01",
        icd_language="en",
        search_rank=1,
    )
    db_session.add(candidate)
    db_session.flush()

    decision = ICDDecision(
        id=uuid.uuid4(),
        fact_id=fact.id,
        candidate_id=candidate.id,
        mapping_status="suggested",  # Not yet accepted!
        needs_review=False,
        icd_release="2026-01",
        icd_language="en",
    )
    db_session.add(decision)
    db_session.commit()

    service = ExportService(db_session)
    headers = service._get_facts_csv_headers()
    assert len(headers) == 25
    assert headers[0] == "patient_id"
    assert headers[2] == "snapshot_id"
    assert headers[7] == "unit"
    assert headers[-1] == "icd_needs_review"

    # Negative number preservation vs formula injection protection
    assert service._sanitize_value("-1.5") == "-1.5"
    assert service._sanitize_value("-1234") == "-1234"
    assert service._sanitize_value("=SUM(A1:A10)").startswith("'")
    assert service._sanitize_value("+SUM(A1:A10)").startswith("'")
    assert service._sanitize_value("-SUM(A1:A10)").startswith("'")
    assert service._sanitize_value("@cmd").startswith("'")

    # Export via HTTP endpoint (default: unaccepted ICD code must NOT be exported)
    export_resp = client.get(f"/export/facts/{patient.id}")
    assert export_resp.status_code == 200
    rows = list(csv.DictReader(io.StringIO(export_resp.text.lstrip("\ufeff"))))
    assert len(rows) == 1
    assert rows[0]["value"] == "Essential hypertension"
    assert rows[0]["icd_code"] == ""  # Excluded because not accepted
    assert rows[0]["snapshot_id"] == "snap-123"

    # Export with include_unaccepted_icd=true
    export_inc_resp = client.get(f"/export/facts/{patient.id}?include_unaccepted_icd=true")
    rows_inc = list(csv.DictReader(io.StringIO(export_inc_resp.text.lstrip("\ufeff"))))
    assert rows_inc[0]["icd_code"] == "BA00"


def test_demo_auth_and_csrf_middleware(client, monkeypatch):
    """Test HTTP Basic auth enforcement when credentials are set and CSRF origin protection."""
    monkeypatch.setattr(settings, "demo_auth_username", "reviewer")
    monkeypatch.setattr(settings, "demo_auth_password", "secret123")

    # Without auth -> 401
    unauth_resp = client.get("/patients/")
    assert unauth_resp.status_code == 401

    # Health check remains accessible for container probes
    assert client.get("/health").status_code == 200

    # With valid Basic auth -> 200
    token = base64.b64encode(b"reviewer:secret123").decode("ascii")
    auth_headers = {"Authorization": f"Basic {token}"}
    auth_resp = client.get("/patients/", headers=auth_headers)
    assert auth_resp.status_code == 200

    # Cross-origin POST -> 403 CSRF blocked
    csrf_headers = {
        "Authorization": f"Basic {token}",
        "Origin": "https://evil.example.com",
    }
    csrf_resp = client.post("/patients/", data={"display_name": "Evil"}, headers=csrf_headers)
    assert csrf_resp.status_code == 403
