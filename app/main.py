import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.endpoints import router as api_router
from app.core.config import settings
from app.core.ratelimit import check_storage_available, check_upload_rate, limiter
from app.core.redis_backend import manager as redis_manager
from app.services.file_service import cleanup_old_files

logger = logging.getLogger("convertly")
if not logger.handlers:   # uvicorn tidak menampilkan log INFO milik aplikasi secara default
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     [convertly] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


async def _cleanup_loop():
    """Hapus file upload/hasil yang sudah kedaluwarsa secara berkala."""
    while True:
        try:
            removed = await asyncio.to_thread(cleanup_old_files)
            limiter.purge()
            if removed:
                logger.info("Cleanup: %d file kedaluwarsa dihapus", removed)
        except Exception:
            logger.exception("Cleanup gagal")
        await asyncio.sleep(settings.CLEANUP_INTERVAL_MINUTES * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(settings.UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(settings.RESULT_FOLDER, exist_ok=True)
    backend = await asyncio.to_thread(redis_manager.backend_name)
    logger.info("Penghitung rate limit: %s", backend)
    cleanup_task = asyncio.create_task(_cleanup_loop())
    yield
    cleanup_task.cancel()


app = FastAPI(
    title="Convertly API",
    description="Backend untuk file processing (PDF & gambar)",
    version="1.1.0",
    lifespan=lifespan,
)

UPLOAD_PATH = "/api/v1/upload"


def _upload_checks(request: Request) -> None:
    check_upload_rate(request)
    check_storage_available()


@app.middleware("http")
async def guard_uploads(request: Request, call_next):
    """Tolak upload yang tidak wajar SEBELUM body dibaca: terlalu besar, terlalu sering, atau disk penuh.
    (Didaftarkan sebelum CORS agar respons penolakan tetap membawa header CORS.)"""
    if request.method == "POST" and request.url.path == UPLOAD_PATH:
        try:
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > settings.MAX_REQUEST_MB * 1024 * 1024:
                raise HTTPException(
                    status_code=413,
                    detail=f"Ukuran upload melebihi batas {settings.MAX_REQUEST_MB:g} MB per permintaan.",
                )
            await asyncio.to_thread(_upload_checks, request)   # bisa menyentuh Redis: jangan blokir event loop
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Retry-After"],
)

app.include_router(api_router, prefix="/api/v1", tags=["Files"])


@app.get("/")
def read_root():
    return {
        "status": "online",
        "message": "Convertly API is running smoothly!",
        "version": app.version,
    }
