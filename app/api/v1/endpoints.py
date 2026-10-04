import logging
import mimetypes
import os
from typing import Any, Callable, List

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.ratelimit import (
    FILE_CAPPED_ACTIONS,
    action_cost,
    check_download_rate,
    check_process_rate,
    check_upload_volume,
    client_ip,
    job_guard,
)
from app.core.security import (
    clean_download_name,
    is_safe_filename,
    result_path,
    upload_path,
)
from app.services import (
    image_service, ocr_service, office_service, pdf_service, photo_service, stamp_service,
)
from app.services.file_service import UploadRejected, remove_file, save_upload_file, zip_results
from app.services.output import Output

logger = logging.getLogger("convertly")
router = APIRouter()

IMAGE_EXTS = {".jpg", ".png", ".webp"}
PDF_EXTS = {".pdf"}
DOCX_EXTS = {".docx"}
PPTX_EXTS = {".pptx"}

# action -> (fungsi, tipe input yang diterima)
# Aksi "per file": dijalankan sekali untuk tiap file, menghasilkan satu hasil per file.
PER_FILE_ACTIONS: dict[str, tuple[Callable[[str, dict], Output], set[str]]] = {
    "compress": (image_service.compress_image, IMAGE_EXTS),
    "resize": (image_service.resize_image, IMAGE_EXTS),
    "convert": (image_service.convert_image, IMAGE_EXTS),
    "compress-pdf": (pdf_service.compress_pdf, PDF_EXTS),
    "split": (pdf_service.split_pdf, PDF_EXTS),
    "rotate": (pdf_service.rotate_pdf, PDF_EXTS),
    "protect": (pdf_service.protect_pdf, PDF_EXTS),
    "unlock": (pdf_service.unlock_pdf, PDF_EXTS),
    "pdf-to-image": (pdf_service.pdf_to_images, PDF_EXTS),
    "pdf-to-docx": (office_service.pdf_to_docx, PDF_EXTS),
    "docx-to-pdf": (office_service.docx_to_pdf, DOCX_EXTS),
    "pptx-to-pdf": (office_service.pptx_to_pdf, PPTX_EXTS),
    "ocr-pdf": (ocr_service.ocr_pdf, PDF_EXTS),
    "ocr-text": (ocr_service.ocr_text, PDF_EXTS | IMAGE_EXTS),
    "page-numbers": (stamp_service.add_page_numbers, PDF_EXTS),
    "watermark": (stamp_service.add_watermark, PDF_EXTS),
    "id-photo": (photo_service.id_photo, IMAGE_EXTS),
}
# Aksi "gabungan": semua file masuk, satu file keluar (urutan file = urutan hasil).
MULTI_FILE_ACTIONS: dict[str, tuple[Callable[[list[str], dict], Output], set[str], int]] = {
    "merge": (pdf_service.merge_pdfs, PDF_EXTS | IMAGE_EXTS, 2),
    "pdf": (image_service.images_to_pdf, IMAGE_EXTS, 1),
}


@router.get("/limits")
def get_limits():
    return {
        "max_upload_mb": settings.MAX_UPLOAD_MB,
        "max_files": settings.MAX_FILES_PER_UPLOAD,
        "file_ttl_minutes": settings.FILE_TTL_MINUTES,
        "docx_to_pdf_available": office_service.available_kinds()[".docx"],
        "pptx_to_pdf_available": office_service.available_kinds()[".pptx"],
        "ocr_available": ocr_service.is_available(),
        "max_ocr_pages": settings.MAX_OCR_PAGES,
    }


@router.post("/upload")
def upload_files(request: Request, files: List[UploadFile] = File(...)):
    if len(files) > settings.MAX_FILES_PER_UPLOAD:
        raise HTTPException(
            status_code=400,
            detail=f"Maksimal {settings.MAX_FILES_PER_UPLOAD} file per upload.",
        )

    uploaded, rejected = [], []
    for file in files:
        try:
            uploaded.append(save_upload_file(file))
        except UploadRejected as e:
            rejected.append({"original_name": file.filename, "reason": str(e)})
        except Exception:
            logger.exception("Upload gagal: %s", file.filename)
            rejected.append({"original_name": file.filename, "reason": "Gagal menyimpan file."})

    if not uploaded:
        reason = rejected[0]["reason"] if rejected else "Tidak ada file valid yang diupload."
        raise HTTPException(status_code=400, detail=reason)

    # Ukuran total (Content-Length bisa dipalsukan/dihilangkan) dan kuota volume per jam
    total_mb = sum(f["size"] for f in uploaded) / (1024 * 1024)
    try:
        if total_mb > settings.MAX_REQUEST_MB:
            raise HTTPException(status_code=413, detail=f"Total ukuran file melebihi {settings.MAX_REQUEST_MB:g} MB per upload.")
        check_upload_volume(client_ip(request), total_mb)
    except HTTPException:
        for f in uploaded:
            remove_file(upload_path(f["saved_name"]))
        raise

    return {
        "status": "success",
        "message": f"{len(uploaded)} file(s) uploaded successfully",
        "data": uploaded,
        "rejected": rejected,
    }


class ProcessRequest(BaseModel):
    action: str
    filenames: List[str] = Field(min_length=1, max_length=settings.MAX_FILES_PER_UPLOAD)
    original_names: List[str] = []   # sejajar dengan filenames, untuk menamai file hasil
    options: dict[str, Any] = {}


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def _download_name(original: str | None, out: Output) -> str:
    stem = os.path.splitext(clean_download_name(original, "convertly"))[0]
    return f"{stem}{out.suffix}{_ext(out.name)}"


def _result_info(out: Output, original: str | None, input_index: int | None) -> dict:
    size = os.path.getsize(out.path) if os.path.exists(out.path) else 0
    return {
        "input_index": input_index,   # None untuk aksi gabungan (merge, image->pdf)
        "original_name": original,
        "processed_file": out.name,
        "processed_size": size,
        "download_name": _download_name(original, out),
        "note": out.note,
    }


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return "File tidak ditemukan atau sudah kedaluwarsa. Upload ulang."
    if isinstance(exc, ValueError):
        return str(exc)
    logger.error("Proses gagal", exc_info=exc)
    return "Gagal memproses file."


@router.post("/process")
def process_files(request: ProcessRequest, http_request: Request):
    action = request.action
    if action not in PER_FILE_ACTIONS and action not in MULTI_FILE_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Aksi tidak dikenali: {action}")

    for name in request.filenames:
        if not is_safe_filename(name):
            raise HTTPException(status_code=400, detail="Nama file tidak valid.")

    if action in FILE_CAPPED_ACTIONS and len(request.filenames) > settings.MAX_HEAVY_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Alat ini memakai banyak tenaga server: maksimal {settings.MAX_HEAVY_FILES_PER_REQUEST} file per proses.",
        )

    files_factor = len(request.filenames) if action in PER_FILE_ACTIONS else 1
    ip = check_process_rate(http_request, action_cost(action) * files_factor)

    originals = list(request.original_names) + [None] * len(request.filenames)
    originals = originals[: len(request.filenames)]

    results, errors = [], []
    with job_guard.slot(ip):
        _run_action(request, action, originals, results, errors)

    if not results:
        raise HTTPException(
            status_code=400,
            detail=errors[0]["error"] if errors else "Gagal memproses file.",
        )
    return {"status": "success", "results": results, "errors": errors}


def _run_action(request: ProcessRequest, action: str, originals: list, results: list, errors: list) -> None:
    try:
        if action in MULTI_FILE_ACTIONS:
            fn, allowed, min_files = MULTI_FILE_ACTIONS[action]
            if len(request.filenames) < min_files:
                raise HTTPException(
                    status_code=400, detail=f"Pilih minimal {min_files} file untuk aksi ini."
                )
            if any(_ext(n) not in allowed for n in request.filenames):
                raise HTTPException(status_code=400, detail="Tipe file tidak sesuai untuk aksi ini.")
            try:
                out = fn(request.filenames, request.options)
                results.append(_result_info(out, originals[0], None))
            except Exception as e:
                errors.append({"input_index": None, "original_name": None, "error": _friendly_error(e)})
        else:
            fn, allowed = PER_FILE_ACTIONS[action]
            for index, (name, original) in enumerate(zip(request.filenames, originals)):
                if _ext(name) not in allowed:
                    errors.append({"input_index": index, "original_name": original, "error": "Tipe file tidak sesuai untuk alat ini."})
                    continue
                try:
                    out = fn(name, request.options)
                    results.append(_result_info(out, original, index))
                except Exception as e:
                    errors.append({"input_index": index, "original_name": original, "error": _friendly_error(e)})
    finally:
        # File upload hanya dipakai sekali; hasil tetap tersedia sampai TTL habis
        for name in request.filenames:
            remove_file(upload_path(name))


class ZipItem(BaseModel):
    file: str            # nama file hasil (processed_file)
    name: str | None = None   # nama di dalam ZIP (download_name)


class ZipRequest(BaseModel):
    items: List[ZipItem] = Field(min_length=1, max_length=50)


@router.post("/zip")
def zip_files(request: ZipRequest, http_request: Request):
    """Gabungkan beberapa file hasil jadi satu ZIP; ZIP diunduh lewat /download seperti biasa."""
    check_process_rate(http_request, 1)
    entries = []
    for item in request.items:
        if not is_safe_filename(item.file):
            raise HTTPException(status_code=400, detail="Nama file tidak valid.")
        path = result_path(item.file)
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="Sebagian file sudah kedaluwarsa. Proses ulang.")
        entries.append((path, clean_download_name(item.name, item.file)))

    try:
        out = zip_results(entries)
    except Exception:
        logger.exception("Gagal membuat ZIP")
        raise HTTPException(status_code=500, detail="Gagal membuat ZIP.")
    return {
        "processed_file": out.name,
        "processed_size": os.path.getsize(out.path),
        "download_name": "convertly_hasil.zip",
    }


@router.get("/download/{filename}")
def download_file(http_request: Request, filename: str, name: str | None = Query(default=None, max_length=200)):
    check_download_rate(http_request)
    if not is_safe_filename(filename):
        raise HTTPException(status_code=404, detail="File not found or expired")
    file_path = result_path(filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found or expired")

    download_name = clean_download_name(name, filename)
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(
        path=file_path, filename=download_name, media_type=media_type,
        # File pribadi: jangan disimpan cache perantara, dan jangan biarkan browser menebak tipe isinya
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
