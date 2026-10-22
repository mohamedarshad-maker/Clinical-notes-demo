import sys
print("="*70)
print("Starting Clinical Notes PoC UI...")
print("="*70)

# Set up path
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

# Import app
from main import app
import uvicorn

print("\nServer starting at: http://localhost:8000")
print("\nYou can:")
print("  - View patient list")  
print("  - See UI structure")
print("  - Navigate pages")
print("\nPress CTRL+C to stop")
print("="*70 + "\n")

uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
