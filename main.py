"""Main FastAPI application entry point."""
import base64
import binascii
from contextlib import asynccontextmanager
import logging
import secrets
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from app.config import settings
from app.database import engine, Base, SessionLocal
from app.services.processing import recover_stale_jobs

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database tables and recover any interrupted jobs on startup."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        recover_stale_jobs(db, force_all_active=True)
    except Exception as e:
        logger.warning(f"Startup stale job recovery skipped: {e}")
    finally:
        db.close()
    yield


app = FastAPI(
    title="Clinical Notes Extraction PoC",
    version="1.0.0",
    description="Extract structured clinical data from documents with WHO ICD-11 mapping",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.app_env == "development" else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_and_csrf_middleware(request: Request, call_next):
    """
    Enforce:
    1. HTTP Basic demo access gate (fails closed in production if credentials missing;
       enforced whenever DEMO_AUTH_USERNAME and DEMO_AUTH_PASSWORD are configured).
    2. Same-origin check for state-changing requests (POST, PATCH, DELETE).
    """
    path = request.url.path

    # Allow health check and static assets without Basic Auth gate
    if path != "/health" and not path.startswith("/static"):
        if settings.app_env == "production" and not settings.is_demo_auth_configured:
            return JSONResponse(
                {"detail": "Demo authentication credentials are required in production mode."},
                status_code=503
            )

        if settings.is_demo_auth_configured:
            auth_header = request.headers.get("Authorization") or ""
            authorized = False
            if auth_header.lower().startswith("basic "):
                encoded = auth_header[6:].strip()
                try:
                    decoded = base64.b64decode(encoded).decode("utf-8")
                    username, _, password = decoded.partition(":")
                    user_ok = secrets.compare_digest(username, settings.effective_demo_username)
                    pass_ok = secrets.compare_digest(password, settings.effective_demo_password)
                    authorized = user_ok and pass_ok
                except (binascii.Error, UnicodeDecodeError):
                    authorized = False

            if not authorized:
                return Response(
                    content="Unauthorized: Demo credentials required",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Clinical Notes PoC"'}
                )

    # Same-origin check for state-changing mutations
    if request.method in {"POST", "PATCH", "DELETE"}:
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if origin:
            parsed_origin = urlparse(origin)
            origin_host = (parsed_origin.hostname or "").lower()
            request_host = (request.url.hostname or "").lower()
            allowed_hosts = {request_host, "localhost", "127.0.0.1", "testserver"}
            if origin_host and origin_host not in allowed_hosts:
                return JSONResponse(
                    {"detail": "Cross-origin state-changing request blocked (CSRF protection)."},
                    status_code=403
                )

    return await call_next(request)


# Static files and templates
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/health")
async def health_check():
    """Health check endpoint with configuration readiness indicators."""
    return {
        "status": "healthy",
        "environment": settings.app_env,
        "demo_only": settings.demo_only,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "llm_configured": settings.is_llm_configured,
        "who_icd_configured": settings.is_who_icd_configured,
        "storage_configured": settings.is_any_storage_configured,
        "storage_provider": settings.storage_provider,
        "ocr_configured": settings.is_ocr_configured,
        "ocr_provider": settings.ocr_provider,
        "tesseract_configured": settings.is_tesseract_configured
    }


# Register routes
from app.routes import patients, documents, processing, export, review

app.include_router(patients.router)
app.include_router(documents.router)
app.include_router(processing.router)
app.include_router(export.router)
app.include_router(review.router)


@app.get("/")
async def root(request: Request):
    """Root endpoint - redirect to patient list."""
    return RedirectResponse(url="/patients")


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.app_env == "development"
    )
