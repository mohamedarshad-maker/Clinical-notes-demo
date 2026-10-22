"""Clinical extraction schemas for 19 categories."""
from typing import List, Optional, Literal, Dict, Any, Union
from pydantic import BaseModel, Field, ConfigDict


# Category group assignments
CATEGORY_GROUPS = {
    "A": [1, 2, 3, 4, 5, 6],
    "B": [7, 8, 9, 10, 11, 12],
    "C": [13, 14, 15, 16, 17, 18, 19],
}

# Canonical category definitions (number -> {key, name, display_name, group, subfields})
CATEGORY_DEFINITIONS: Dict[int, Dict[str, Any]] = {
    1: {
        "key": "patient_details",
        "name": "Patient details",
        "display_name": "Patient details",
        "group": "A",
        "subfields": ["documented_name", "patient_identifiers", "age", "date_of_birth", "sex_gender", "sex_gender_label"],
    },
    2: {
        "key": "visit_details",
        "name": "Visit details",
        "display_name": "Visit details",
        "group": "A",
        "subfields": ["encounter_id", "encounter_date", "note_date", "note_type", "facility", "department", "clinician_name", "clinician_role", "clinician_specialty", "provider_identifier"],
    },
    3: {
        "key": "reason_for_visit",
        "name": "Reason for visit",
        "display_name": "Reason for visit",
        "group": "A",
        "subfields": ["main_complaint", "additional_complaints", "visit_purpose"],
    },
    4: {
        "key": "symptoms",
        "name": "Symptoms",
        "display_name": "Symptoms",
        "group": "A",
        "subfields": ["symptom", "site", "laterality", "onset", "duration", "severity", "frequency"],
    },
    5: {
        "key": "medical_history",
        "name": "Medical history",
        "display_name": "Medical history",
        "group": "A",
        "subfields": ["condition", "onset", "date", "status", "previous_admission", "admission_reason"],
    },
    6: {
        "key": "surgical_history",
        "name": "Surgical history",
        "display_name": "Surgical history",
        "group": "A",
        "subfields": ["procedure", "site", "laterality", "date", "outcome", "complications"],
    },
    7: {
        "key": "family_history",
        "name": "Family history",
        "display_name": "Family history",
        "group": "B",
        "subfields": ["relative", "relationship", "condition", "age_at_onset", "timing"],
    },
    8: {
        "key": "social_history",
        "name": "Social history",
        "display_name": "Social history",
        "group": "B",
        "subfields": ["category", "detail", "status", "frequency", "tobacco", "alcohol", "substances", "occupation", "living_situation", "support", "barriers"],
    },
    9: {
        "key": "vital_signs",
        "name": "Vital signs",
        "display_name": "Vital signs",
        "group": "B",
        "subfields": ["measurement_name", "value", "unit", "measurement_datetime"],
    },
    10: {
        "key": "examination_findings",
        "name": "Examination findings",
        "display_name": "Examination findings",
        "group": "B",
        "subfields": ["body_system", "site", "finding", "normal_abnormal", "severity", "laterality"],
    },
    11: {
        "key": "diagnoses_assessment",
        "name": "Diagnoses and assessment",
        "display_name": "Diagnoses and assessment",
        "group": "B",
        "subfields": ["diagnosis", "certainty", "status", "clinician_assessment", "original_codes"],
    },
    12: {
        "key": "medications",
        "name": "Medications",
        "display_name": "Medications",
        "group": "B",
        "subfields": ["name", "strength", "dose", "dose_unit", "route", "frequency", "as_needed", "as_needed_indication", "duration", "start_date", "end_date", "status", "change_reason", "prescriber"],
    },
    13: {
        "key": "allergies",
        "name": "Allergies",
        "display_name": "Allergies",
        "group": "C",
        "subfields": ["substance", "reaction", "severity", "allergy_status"],
    },
    14: {
        "key": "tests_investigations",
        "name": "Tests and investigations",
        "display_name": "Tests and investigations",
        "group": "C",
        "subfields": ["test_name", "specimen", "order_date", "performed_date", "status", "result", "unit", "reference_range", "interpretation", "imaging_findings"],
    },
    15: {
        "key": "procedures_treatments",
        "name": "Procedures and treatments",
        "display_name": "Procedures and treatments",
        "group": "C",
        "subfields": ["name", "site", "indication", "status", "clinician", "date", "frequency", "sessions", "duration", "outcome"],
    },
    16: {
        "key": "response_adverse_events",
        "name": "Treatment response and adverse events",
        "display_name": "Treatment response and adverse events",
        "group": "C",
        "subfields": ["event", "event_type", "onset", "severity", "associated_treatment", "causal_certainty", "action_taken", "outcome"],
    },
    17: {
        "key": "care_plan_followup",
        "name": "Care plan and follow-up",
        "display_name": "Care plan and follow-up",
        "group": "C",
        "subfields": ["action", "reason", "recommending_clinician", "responsible_person", "referral_destination", "timeframe", "date", "conditions", "completion_status"],
    },
    18: {
        "key": "patient_instructions",
        "name": "Patient instructions",
        "display_name": "Patient instructions",
        "group": "C",
        "subfields": ["topic", "instruction", "timing", "category", "warning_signs", "contact_return_instructions"],
    },
    19: {
        "key": "discharge_information",
        "name": "Discharge information",
        "display_name": "Discharge information",
        "group": "C",
        "subfields": ["admission_date", "discharge_date", "admission_reason", "hospital_course", "discharge_condition", "discharge_destination", "discharge_medications", "pending_tests", "follow_up"],
    },
}

CATEGORY_KEY_TO_NUMBER: Dict[str, int] = {
    v["key"]: k for k, v in CATEGORY_DEFINITIONS.items()
}


class ClinicalEvidence(BaseModel):
    """Evidence linking fact to source document."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    page_number: int = 1
    block_id: Optional[str] = None
    quote: str


class BaseClinicalEntry(BaseModel):
    """Shared clinical metadata across all 19 category entries."""

    model_config = ConfigDict(extra="forbid")

    availability: Literal["documented",
                          "not_documented", "unreadable"] = "documented"
    assertion: Optional[Literal["present", "negated",
                                "uncertain", "unknown"]] = "present"
    temporality: Optional[Literal["current", "historical",
                                  "planned", "resolved", "unknown"]] = "current"
    experiencer: Optional[Literal["patient",
                                  "family_member", "other", "unknown"]] = "patient"
    event_time: Optional[str] = None
    original_time_expression: Optional[str] = None
    origin: Optional[Literal["extraction", "derived"]] = "extraction"
    evidence: List[ClinicalEvidence] = Field(default_factory=list)


class PatientDetailsEntry(BaseClinicalEntry):
    """1. Patient identity and demographics."""

    documented_name: Optional[str] = None
    patient_identifiers: Optional[List[str]] = Field(
        default=None, description="MRN or other IDs")
    age: Optional[str] = None
    date_of_birth: Optional[str] = None
    sex_gender: Optional[str] = None
    sex_gender_label: Optional[str] = Field(
        default=None, description="Original label used")


class VisitDetailsEntry(BaseClinicalEntry):
    """2. Encounter and note metadata."""

    encounter_id: Optional[str] = None
    encounter_date: Optional[str] = None
    note_date: Optional[str] = None
    note_type: Optional[str] = None
    facility: Optional[str] = None
    department: Optional[str] = None
    clinician_name: Optional[str] = None
    clinician_role: Optional[str] = None
    clinician_specialty: Optional[str] = None
    provider_identifier: Optional[str] = None


class ReasonForVisitEntry(BaseClinicalEntry):
    """3. Chief complaint and visit purpose."""

    main_complaint: Optional[str] = None
    additional_complaints: Optional[List[str]] = None
    visit_purpose: Optional[str] = None


class SymptomEntry(BaseClinicalEntry):
    """4. Individual symptom with details."""

    symptom: str
    site: Optional[str] = None
    laterality: Optional[str] = None
    onset: Optional[str] = None
    duration: Optional[str] = None
    severity: Optional[str] = None
    frequency: Optional[str] = None


class MedicalHistoryEntry(BaseClinicalEntry):
    """5. Past medical conditions."""

    condition: str
    onset: Optional[str] = None
    date: Optional[str] = None
    status: Optional[str] = Field(
        default=None, description="active, resolved, etc.")
    previous_admission: Optional[bool] = None
    admission_reason: Optional[str] = None
    temporality: Optional[Literal["current", "historical",
                                  "planned", "resolved", "unknown"]] = "historical"


class SurgicalHistoryEntry(BaseClinicalEntry):
    """6. Past surgical procedures."""

    procedure: str
    site: Optional[str] = None
    laterality: Optional[str] = None
    date: Optional[str] = None
    outcome: Optional[str] = None
    complications: Optional[str] = None
    temporality: Optional[Literal["current", "historical",
                                  "planned", "resolved", "unknown"]] = "historical"


class FamilyHistoryEntry(BaseClinicalEntry):
    """7. Family medical history."""

    relative: Optional[str] = None
    relationship: Optional[str] = None
    condition: str
    age_at_onset: Optional[str] = None
    timing: Optional[str] = None
    experiencer: Optional[Literal["patient",
                                  "family_member", "other", "unknown"]] = "family_member"


class SocialHistoryEntry(BaseClinicalEntry):
    """8. Social and lifestyle factors."""

    category: str = Field(
        default="general",
        description="tobacco, alcohol, substances, occupation, living_situation, support, barriers",
    )
    detail: Optional[str] = None
    status: Optional[str] = None
    frequency: Optional[str] = None
    tobacco: Optional[str] = None
    alcohol: Optional[str] = None
    substances: Optional[str] = None
    occupation: Optional[str] = None
    living_situation: Optional[str] = None
    support: Optional[str] = None
    barriers: Optional[str] = None


class VitalSignEntry(BaseClinicalEntry):
    """9. Vital sign measurement."""

    measurement_name: str
    value: str
    unit: Optional[str] = None
    measurement_datetime: Optional[str] = None


class ExaminationFindingEntry(BaseClinicalEntry):
    """10. Physical examination finding."""

    body_system: Optional[str] = None
    site: Optional[str] = None
    finding: str
    normal_abnormal: Optional[Literal["normal", "abnormal", "unknown"]] = None
    severity: Optional[str] = None
    laterality: Optional[str] = None


class OriginalCodeEntry(BaseModel):
    """Original code written in source document."""

    model_config = ConfigDict(extra="allow")

    code: str
    coding_system: str = "unknown"
    description: Optional[str] = None


class DiagnosisEntry(BaseClinicalEntry):
    """11. Diagnosis or clinical assessment."""

    diagnosis: str
    certainty: Optional[Literal["confirmed",
                                "suspected", "rule_out", "unknown"]] = None
    status: Optional[str] = Field(
        default=None, description="current, history, resolved")
    clinician_assessment: Optional[str] = None
    original_codes: Optional[List[Union[OriginalCodeEntry, Dict[str, Any]]]] = Field(
        default=None, description="Codes documented in source"
    )


class MedicationEntry(BaseClinicalEntry):
    """12. Medication with complete details."""

    name: str
    strength: Optional[str] = None
    dose: Optional[str] = None
    dose_unit: Optional[str] = None
    route: Optional[str] = None
    frequency: Optional[str] = None
    as_needed: Optional[bool] = None
    as_needed_indication: Optional[str] = None
    duration: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: Optional[str] = Field(
        default=None, description="current, started, stopped, changed, planned")
    change_reason: Optional[str] = None
    prescriber: Optional[str] = None


class AllergyEntry(BaseClinicalEntry):
    """13. Allergy or adverse reaction."""

    substance: Optional[str] = None
    reaction: Optional[str] = None
    severity: Optional[str] = None
    allergy_status: Optional[str] = Field(
        default=None, description="confirmed, no_known_allergies, unknown")


class TestInvestigationEntry(BaseClinicalEntry):
    """14. Lab test or imaging study."""

    test_name: str
    specimen: Optional[str] = None
    order_date: Optional[str] = None
    performed_date: Optional[str] = None
    status: Optional[Literal["ordered", "pending",
                             "completed", "cancelled", "unknown"]] = None
    result: Optional[str] = None
    unit: Optional[str] = None
    reference_range: Optional[str] = None
    interpretation: Optional[str] = None
    imaging_findings: Optional[str] = None


class ProcedureTreatmentEntry(BaseClinicalEntry):
    """15. Procedure or treatment."""

    name: str
    site: Optional[str] = None
    indication: Optional[str] = None
    status: Optional[Literal["planned",
                             "performed", "cancelled", "unknown"]] = None
    clinician: Optional[str] = None
    date: Optional[str] = None
    frequency: Optional[str] = None
    sessions: Optional[str] = None
    duration: Optional[str] = None
    outcome: Optional[str] = None


class ResponseAdverseEventEntry(BaseClinicalEntry):
    """16. Treatment response or adverse event."""

    event: str
    event_type: Optional[Literal["improvement",
                                 "worsening", "adverse_event", "unknown"]] = None
    onset: Optional[str] = None
    severity: Optional[str] = None
    associated_treatment: Optional[str] = None
    causal_certainty: Optional[Literal["confirmed",
                                       "probable", "possible", "unlikely", "unknown"]] = None
    action_taken: Optional[str] = None
    outcome: Optional[str] = None


class CarePlanFollowUpEntry(BaseClinicalEntry):
    """17. Care plan and follow-up recommendations."""

    action: str
    reason: Optional[str] = None
    recommending_clinician: Optional[str] = None
    responsible_person: Optional[str] = None
    referral_destination: Optional[str] = None
    timeframe: Optional[str] = None
    date: Optional[str] = None
    conditions: Optional[str] = None
    completion_status: Optional[str] = Field(
        default=None, description="Only when documented")
    temporality: Optional[Literal["current", "historical",
                                  "planned", "resolved", "unknown"]] = "planned"


class PatientInstructionEntry(BaseClinicalEntry):
    """18. Patient education and instructions."""

    topic: str
    instruction: str
    timing: Optional[str] = None
    category: Optional[str] = Field(
        default=None, description="medication, diet, activity, wound, warnings, contact"
    )
    warning_signs: Optional[str] = None
    contact_return_instructions: Optional[str] = None


class DischargeInformationEntry(BaseClinicalEntry):
    """19. Discharge summary details."""

    admission_date: Optional[str] = None
    discharge_date: Optional[str] = None
    admission_reason: Optional[str] = None
    hospital_course: Optional[str] = None
    discharge_condition: Optional[str] = None
    discharge_destination: Optional[str] = None
    discharge_medications: Optional[List[str]] = None
    pending_tests: Optional[List[str]] = None
    follow_up: Optional[str] = None


class CategoryAvailability(BaseModel):
    """Marks category as not documented or unreadable after processing."""

    model_config = ConfigDict(extra="forbid")

    availability: Literal["not_documented", "unreadable"] = "not_documented"


# Group A output (categories 1-6)
class GroupAOutput(BaseModel):
    """Extraction output for Group A."""

    model_config = ConfigDict(extra="forbid")

    patient_details: Optional[Union[List[PatientDetailsEntry],
                                    PatientDetailsEntry, CategoryAvailability]] = None
    visit_details: Optional[Union[List[VisitDetailsEntry],
                                  VisitDetailsEntry, CategoryAvailability]] = None
    reason_for_visit: Optional[Union[List[ReasonForVisitEntry],
                                     ReasonForVisitEntry, CategoryAvailability]] = None
    symptoms: Union[List[SymptomEntry],
                    CategoryAvailability] = Field(default_factory=list)
    medical_history: Union[List[MedicalHistoryEntry],
                           CategoryAvailability] = Field(default_factory=list)
    surgical_history: Union[List[SurgicalHistoryEntry],
                            CategoryAvailability] = Field(default_factory=list)


# Group B output (categories 7-12)
class GroupBOutput(BaseModel):
    """Extraction output for Group B."""

    model_config = ConfigDict(extra="forbid")

    family_history: Union[List[FamilyHistoryEntry],
                          CategoryAvailability] = Field(default_factory=list)
    social_history: Union[List[SocialHistoryEntry],
                          CategoryAvailability] = Field(default_factory=list)
    vital_signs: Union[List[VitalSignEntry],
                       CategoryAvailability] = Field(default_factory=list)
    examination_findings: Union[List[ExaminationFindingEntry],
                                CategoryAvailability] = Field(default_factory=list)
    diagnoses_assessment: Union[List[DiagnosisEntry],
                                CategoryAvailability] = Field(default_factory=list)
    medications: Union[List[MedicationEntry],
                       CategoryAvailability] = Field(default_factory=list)


# Group C output (categories 13-19)
class GroupCOutput(BaseModel):
    """Extraction output for Group C."""

    model_config = ConfigDict(extra="forbid")

    allergies: Union[List[AllergyEntry],
                     CategoryAvailability] = Field(default_factory=list)
    tests_investigations: Union[List[TestInvestigationEntry],
                                CategoryAvailability] = Field(default_factory=list)
    procedures_treatments: Union[List[ProcedureTreatmentEntry],
                                 CategoryAvailability] = Field(default_factory=list)
    response_adverse_events: Union[List[ResponseAdverseEventEntry],
                                   CategoryAvailability] = Field(default_factory=list)
    care_plan_followup: Union[List[CarePlanFollowUpEntry],
                              CategoryAvailability] = Field(default_factory=list)
    patient_instructions: Union[List[PatientInstructionEntry],
                                CategoryAvailability] = Field(default_factory=list)
    discharge_information: Optional[
        Union[List[DischargeInformationEntry],
              DischargeInformationEntry, CategoryAvailability]
    ] = None


class ExtractionGroupOutput(BaseModel):
    """Output for one extraction group with metadata."""

    model_config = ConfigDict(extra="forbid")

    group_name: str
    categories: List[int]
    data: Union[GroupAOutput, GroupBOutput, GroupCOutput]


class CombinedExtractionOutput(BaseModel):
    """Combined output from all three extraction groups."""

    model_config = ConfigDict(extra="forbid")

    patient_details: Optional[Union[List[PatientDetailsEntry],
                                    PatientDetailsEntry, CategoryAvailability]] = None
    visit_details: Optional[Union[List[VisitDetailsEntry],
                                  VisitDetailsEntry, CategoryAvailability]] = None
    reason_for_visit: Optional[Union[List[ReasonForVisitEntry],
                                     ReasonForVisitEntry, CategoryAvailability]] = None
    symptoms: Union[List[SymptomEntry],
                    CategoryAvailability] = Field(default_factory=list)
    medical_history: Union[List[MedicalHistoryEntry],
                           CategoryAvailability] = Field(default_factory=list)
    surgical_history: Union[List[SurgicalHistoryEntry],
                            CategoryAvailability] = Field(default_factory=list)
    family_history: Union[List[FamilyHistoryEntry],
                          CategoryAvailability] = Field(default_factory=list)
    social_history: Union[List[SocialHistoryEntry],
                          CategoryAvailability] = Field(default_factory=list)
    vital_signs: Union[List[VitalSignEntry],
                       CategoryAvailability] = Field(default_factory=list)
    examination_findings: Union[List[ExaminationFindingEntry],
                                CategoryAvailability] = Field(default_factory=list)
    diagnoses_assessment: Union[List[DiagnosisEntry],
                                CategoryAvailability] = Field(default_factory=list)
    medications: Union[List[MedicationEntry],
                       CategoryAvailability] = Field(default_factory=list)
    allergies: Union[List[AllergyEntry],
                     CategoryAvailability] = Field(default_factory=list)
    tests_investigations: Union[List[TestInvestigationEntry],
                                CategoryAvailability] = Field(default_factory=list)
    procedures_treatments: Union[List[ProcedureTreatmentEntry],
                                 CategoryAvailability] = Field(default_factory=list)
    response_adverse_events: Union[List[ResponseAdverseEventEntry],
                                   CategoryAvailability] = Field(default_factory=list)
    care_plan_followup: Union[List[CarePlanFollowUpEntry],
                              CategoryAvailability] = Field(default_factory=list)
    patient_instructions: Union[List[PatientInstructionEntry],
                                CategoryAvailability] = Field(default_factory=list)
    discharge_information: Optional[
        Union[List[DischargeInformationEntry],
              DischargeInformationEntry, CategoryAvailability]
    ] = None
