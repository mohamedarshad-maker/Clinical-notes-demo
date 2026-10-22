"""Main FastAPI application entry point."""
import asyncio
from contextlib import asynccontextmanager
import logging
import os
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
import httpx
import uvicorn

from app.config import settings
from app.database import engine, Base, SessionLocal
from app.services.processing import recover_stale_jobs

logger = logging.getLogger(__name__)


async def _render_keep_alive_loop():
    """Periodically ping the public Render URL every 10 minutes so the instance stays warm."""
    render_url = (os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if not render_url:
        return
    health_url = f"{render_url}/health"
    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            await asyncio.sleep(600)  # 10 minutes (Render inactivity timeout is 15 minutes)
            try:
                await client.get(health_url)
            except Exception as exc:
                logger.debug(f"Keep-alive ping to {health_url} skipped: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database tables, recover interrupted jobs, and run Render keep-alive loop."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        recover_stale_jobs(db, force_all_active=True)
    except Exception as e:
        logger.warning(f"Startup stale job recovery skipped: {e}")
    finally:
        db.close()

    keep_alive_task = asyncio.create_task(_render_keep_alive_loop())
    try:
        yield
    finally:
        keep_alive_task.cancel()


app = FastAPI(
    title="Clinical Notes Extraction PoC",
    version="1.0.0",
    description="Extract structured clinical data from documents with WHO ICD-11 mapping",
    lifespan=lifespan
)

# GZip compression for large clinical JSON payloads and HTML templates
app.add_middleware(GZipMiddleware, minimum_size=1000)

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
    Enforce same-origin check for state-changing requests (POST, PATCH, DELETE)
    and set static asset cache headers.
    """
    path = request.url.path

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

    response = await call_next(request)
    if path.startswith("/static"):
        response.headers["Cache-Control"] = "public, max-age=86400"
    return response


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
