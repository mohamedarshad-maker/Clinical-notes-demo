# Clinical Notes Extraction PoC

A demonstration system that extracts structured clinical data from documents and provides WHO ICD-11 MMS code suggestions.

## Features

- **Document Processing**: Upload PDFs, images, or paste text
- **OCR Support**: Uses Tesseract for scanned documents
- **Parallel Extraction**: Three parallel LLM calls for 19 clinical categories
- **WHO ICD-11 Integration**: Automated MMS code lookup with OAuth
- **Evidence Tracking**: All facts linked to source documents
- **Editable Tables**: Review and correct extracted data
- **CSV Export**: Export facts and summaries
- **Persistent Storage**: Supabase for files and Postgres for data

## Architecture

### Technology Stack
- **Backend**: Python 3.11, FastAPI
- **Database**: PostgreSQL (via Supabase)
- **Storage**: Supabase Storage (private bucket)
- **Document Parsing**: pdfplumber, pypdfium2, Tesseract
- **LLM**: Gemini or Groq (configurable)
- **Frontend**: Jinja2 templates, vanilla JavaScript

### Key Services

1. **Storage Service** (`app/services/storage.py`): Private file management
2. **Document Parser** (`app/services/document_parser.py`): Text extraction with OCR
3. **LLM Client** (`app/services/llm_client.py`): Unified Gemini/Groq interface
4. **Extraction Service** (`app/services/extraction.py`): Parallel category extraction
5. **WHO ICD Service** (`app/services/who_icd.py`): ICD-11 MMS lookup
6. **Patient Summary** (`app/services/patient_summary.py`): Grounded summaries
7. **Processing Orchestration** (`app/services/processing.py`): Workflow management
8. **Export Service** (`app/services/export.py`): CSV generation

## Setup Instructions

### Prerequisites

1. **Python 3.11+**
2. **PostgreSQL** (or Supabase account)
3. **Tesseract OCR**:
   - Windows: Download from https://github.com/tesseract-ocr/tesseract
   - Linux: `sudo apt-get install tesseract-ocr tesseract-ocr-eng`
   - Mac: `brew install tesseract`

### Local Development (Windows)

1. **Clone and navigate to project**:
   ```bash
   cd Clinical-notes-new
   ```

2. **Create virtual environment**:
   ```bash
   python -m venv venv
   venv\Scripts\activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment**:
   - Copy `.env.example` to `.env`
   - Fill in required credentials:
     - Database URL (Supabase Postgres connection string)
     - Supabase URL and service role key
     - LLM provider (Gemini API key OR Groq API key)
     - WHO ICD client ID and secret (register at https://icd.who.int/icdapi)
     - Tesseract path (if not in system PATH)

5. **Create Supabase bucket**:
   - Log into Supabase dashboard
   - Create a **private** bucket named `clinical-notes-private`

6. **Run database migrations**:
   ```bash
   alembic upgrade head
   ```
   Or let FastAPI create tables automatically in development mode.

7. **Start the application**:
   ```bash
   python main.py
   ```
   Access at: http://localhost:8000

### Docker Deployment

1. **Build image**:
   ```bash
   docker build -t clinical-notes-poc .
   ```

2. **Run container**:
   ```bash
   docker run -p 8000:8000 --env-file .env clinical-notes-poc
   ```

### Render Deployment

1. **Create new Web Service** in Render dashboard

2. **Connect your repository**

3. **Configure**:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`

4. **Add environment variables** from `.env.example`

5. **Deploy**

## Configuration Reference

### Required Credentials

| Variable | Description | Where to Get |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection string | Supabase project settings |
| `SUPABASE_URL` | Supabase project URL | Supabase project settings |
| `SUPABASE_SERVICE_ROLE_KEY` | Service role key | Supabase project API settings |
| `GEMINI_API_KEY` or `GROQ_API_KEY` | LLM API key | Google AI Studio or Groq Console |
| `WHO_ICD_CLIENT_ID` | WHO ICD API client ID | https://icd.who.int/icdapi |
| `WHO_ICD_CLIENT_SECRET` | WHO ICD API client secret | https://icd.who.int/icdapi |

### Optional Configuration

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | LLM provider: `gemini` or `groq` |
| `LLM_MODEL` | (required) | Model name for chosen provider |
| `WHO_ICD_RELEASE` | `2026-01` | ICD-11 MMS release version |
| `MAX_FILES_PER_UPLOAD` | `5` | Maximum files per upload |
| `MAX_FILE_MB` | `10` | Maximum file size in MB |
| `MAX_PDF_PAGES` | `20` | Maximum pages per PDF |

## Usage Workflow

1. **Create a Patient**: Click "New Patient" and enter a name
2. **Upload Documents**: Select PDFs, images, or paste text
3. **Start Analysis**: Click "Start Analysis" to extract clinical data
4. **Review Facts**: View extracted facts organized in 19 categories
5. **Review ICD Codes**: Accept, reject, or refine WHO ICD-11 suggestions
6. **Export Data**: Download CSV files with facts and summary

## Clinical Categories (19)

1. Patient Details
2. Visit Details
3. Reason for Visit
4. Symptoms
5. Medical History
6. Surgical History
7. Family History
8. Social History
9. Vital Signs
10. Examination Findings
11. Diagnoses and Assessment
12. Medications
13. Allergies
14. Tests and Investigations
15. Procedures and Treatments
16. Treatment Response and Adverse Events
17. Care Plan and Follow-up
18. Patient Instructions
19. Discharge Information

## Testing

### Run Tests
```bash
pytest tests/ -v
```

### Test Coverage
```bash
pytest tests/ --cov=app --cov-report=html
```

### Manual Testing

1. **OCR Test**: Upload a scanned PDF or image
2. **LLM Test**: Analyze a patient with documents
3. **WHO ICD Test**: Check status at `/health` endpoint

## Limitations

- **Demo Only**: Not for production clinical use
- **Synthetic Data**: Designed for test/demo data
- **Single Process**: One analysis at a time
- **No Multi-User Auth**: Shared demo access only
- **Context Limits**: Large document sets may exceed model limits
- **No Real-Time Updates**: Background processing requires polling

## Important Files

### Core Application
- `main.py`: FastAPI application entry point
- `app/config.py`: Configuration management
- `app/database.py`: Database setup
- `app/models/`: SQLAlchemy models
- `app/schemas/`: Pydantic schemas (19 clinical categories)
- `app/services/`: Business logic services
- `app/routes/`: API endpoints
- `app/templates/`: Jinja2 HTML templates

### Configuration
- `.env.example`: Environment variables template
- `requirements.txt`: Python dependencies
- `alembic.ini`: Database migrations config
- `Dockerfile`: Container image definition

### Database Models
- `Patient`: Demo patient records
- `Document`: Uploaded files metadata
- `DocumentPage`: Parsed page text and blocks
- `ProcessingJob`: Workflow orchestration
- `ExtractionSnapshot`: Immutable extraction input
- `ExtractionRun`: Individual group extraction
- `ConsolidatedFact`: Extracted clinical facts
- `ReviewRevision`: Edit history
- `ICDCandidate`: WHO ICD-11 candidates
- `ICDDecision`: Reviewer ICD decisions
- `PatientSummary`: Generated descriptions

## Security Notes

- **Private Storage**: All files stored in private Supabase bucket
- **Server-Side Credentials**: LLM and WHO API keys never exposed to browser
- **Demo Access Gate**: Shared password for deployed demo
- **HTTPS Required**: For production deployment
- **No PHI**: Do not use with real patient data under this architecture

## Troubleshooting

### Tesseract Not Found
- Windows: Set `TESSERACT_CMD` in `.env` to full path
- Linux/Mac: Ensure `tesseract` is in PATH

### Database Connection Failed
- Check `DATABASE_URL` format
- Verify Supabase project is active
- Ensure IP is allowed in Supabase settings

### LLM Timeout
- Reduce document count
- Check API key validity
- Verify network connectivity

### WHO ICD Authentication Failed
- Verify client ID and secret
- Check credentials at https://icd.who.int/icdapi
- Ensure scope is `icdapi_access`

## License

This is a demonstration project. See project documentation for usage terms.

## Support

For issues and questions, refer to the project documentation or contact the development team.
