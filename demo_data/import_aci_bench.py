"""
ACI-Bench Demo Data Import Script

This script imports synthetic clinical notes from the ACI-Bench dataset.

Source: https://github.com/wyim/aci-bench
License: Check repository for dataset terms
Attribution: ACI-Bench dataset by Wyim et al.

Usage:
    python demo_data/import_aci_bench.py

Note: Download ACI-Bench dataset first and place in demo_data/aci_bench/
"""
import asyncio
import json
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.orm import Session
from app.database import SessionLocal, engine, Base
from app.models import Patient
from app.services.storage import storage_service
import uuid


async def import_aci_bench_data():
    """Import ACI-Bench dataset."""
    print("ACI-Bench Data Import")
    print("=" * 50)

    # Check if ACI-Bench data exists
    data_dir = Path(__file__).parent / "aci_bench"
    if not data_dir.exists():
        print(f"❌ Data directory not found: {data_dir}")
        print("\nPlease download ACI-Bench dataset from:")
        print("https://github.com/wyim/aci-bench")
        print(f"And place files in: {data_dir}")
        return

    db = SessionLocal()

    try:
        # Create tables
        Base.metadata.create_all(bind=engine)
        print("✓ Database tables created")

        # Look for JSON files
        json_files = list(data_dir.glob("*.json"))

        if not json_files:
            print(f"❌ No JSON files found in {data_dir}")
            return

        print(f"\nFound {len(json_files)} data files")

        # Import up to 100 encounters
        imported_count = 0
        max_imports = 100

        for json_file in json_files[:max_imports]:
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                # Create patient for this encounter
                patient_name = data.get('patient_name', f'Patient {imported_count + 1}')

                patient = Patient(
                    id=uuid.uuid4(),
                    display_name=patient_name,
                    is_demo=True
                )

                db.add(patient)
                db.commit()

                print(f"✓ Imported: {patient_name}")
                imported_count += 1

                # Here you would:
                # 1. Split clinical note into sections
                # 2. Create multiple document records
                # 3. Upload to storage
                # 4. Create variants (TXT, PDF, scanned)
                #
                # This is left as an exercise as it requires:
                # - Access to actual ACI-Bench format
                # - PDF generation libraries
                # - Image rendering for scanned variants

            except Exception as e:
                print(f"⚠ Failed to import {json_file.name}: {e}")
                continue

        print(f"\n{'=' * 50}")
        print(f"Import complete: {imported_count} patients created")
        print("\nNote: Full document upload requires additional processing")
        print("to create TXT, PDF, and scanned variants from the source data.")

    except Exception as e:
        print(f"❌ Import failed: {e}")
        db.rollback()

    finally:
        db.close()


async def create_sample_patients():
    """Create sample patients for testing without ACI-Bench data."""
    print("Creating Sample Test Patients")
    print("=" * 50)

    db = SessionLocal()

    try:
        Base.metadata.create_all(bind=engine)

        sample_patients = [
            "John Doe",
            "Jane Smith",
            "Robert Johnson",
            "Maria Garcia",
            "Michael Chen"
        ]

        for name in sample_patients:
            patient = Patient(
                id=uuid.uuid4(),
                display_name=name,
                is_demo=True
            )
            db.add(patient)
            print(f"✓ Created: {name}")

        db.commit()
        print(f"\n✓ Created {len(sample_patients)} sample patients")
        print("\nYou can now:")
        print("1. Start the application: python main.py")
        print("2. Navigate to http://localhost:8000")
        print("3. Upload clinical documents for any patient")

    except Exception as e:
        print(f"❌ Failed to create patients: {e}")
        db.rollback()

    finally:
        db.close()


if __name__ == "__main__":
    print("Clinical Notes PoC - Demo Data Setup\n")
    print("Options:")
    print("1. Import ACI-Bench dataset (if downloaded)")
    print("2. Create sample test patients only")

    choice = input("\nSelect option (1 or 2): ").strip()

    if choice == "1":
        asyncio.run(import_aci_bench_data())
    elif choice == "2":
        asyncio.run(create_sample_patients())
    else:
        print("Invalid option")
