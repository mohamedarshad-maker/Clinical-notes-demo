"""Quick local setup with SQLite for testing."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import uuid

# Use SQLite for local testing
engine = create_engine("sqlite:///./clinical_notes.db", connect_args={"check_same_thread": False})

from app.database import Base
from app.models import Patient

# Create tables
print("Creating database tables...")
Base.metadata.create_all(bind=engine)
print("[OK] Tables created")

# Create session
SessionLocal = sessionmaker(bind=engine)
db = SessionLocal()

# Create sample patients
print("\nCreating sample patients...")
sample_patients = [
    "John Doe - Diabetes Case",
    "Jane Smith - Cardiac Patient",
    "Robert Johnson - Surgical Follow-up",
    "Maria Garcia - Primary Care Visit",
    "Michael Chen - Emergency Department"
]

for name in sample_patients:
    patient = Patient(
        id=uuid.uuid4(),
        display_name=name,
        is_demo=True
    )
    db.add(patient)
    print(f"[OK] Created: {name}")

db.commit()
db.close()

print(f"\n{'='*60}")
print("SUCCESS: LOCAL SETUP COMPLETE!")
print(f"{'='*60}")
print("\nDatabase: clinical_notes.db (SQLite)")
print(f"Patients created: {len(sample_patients)}")
print("\nNEXT STEPS:")
print("\n1. Start the application:")
print("   python main.py")
print("\n2. Open browser:")
print("   http://localhost:8000")
print("\n3. To configure real services, edit .env file with:")
print("   - Supabase credentials (for file upload)")
print("   - LLM API key (Gemini or Groq - for extraction)")
print("   - WHO ICD-11 credentials (for ICD codes)")
print("\nNOTE: This local SQLite setup is for UI testing only.")
print("      For full functionality, you need the credentials above.")
print("\nSee README.md for complete setup instructions.")
