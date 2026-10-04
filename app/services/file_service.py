import os
import time
import uuid
import zipfile
from fastapi import UploadFile
from app.core.config import settings
from app.services.output import Output, new_result

CHUNK = 1024 * 1024  # 1 MB


UNSUPPORTED_MESSAGE = "Tipe file tidak didukung (PDF, DOCX, PPTX, JPG, PNG, WEBP)."


class UploadRejected(Exception):
    """File ditolak saat upload (dengan alasan yang aman ditampilkan ke user)."""


def detect_extension(head: bytes) -> str | None:
    """Tentukan tipe file dari isi (magic bytes), bukan dari nama/ekstensi dari client."""
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(b"PK\x03\x04"):
        return ".zip"   # zip: jenisnya (docx/pptx) dikonfirmasi dari isinya setelah disimpan
    return None


def detect_office_kind(path: str) -> str | None:
    """'.docx' jika zip berisi word/document.xml, '.pptx' jika berisi ppt/presentation.xml."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return None
    if "[Content_Types].xml" not in names:
        return None
    if "word/document.xml" in names:
        return ".docx"
    if "ppt/presentation.xml" in names:
        return ".pptx"
    return None


def save_upload_file(file: UploadFile) -> dict:
    original_name = os.path.basename((file.filename or "file").replace("\\", "/"))

    head = file.file.read(16)
    file.file.seek(0)
    ext = detect_extension(head)
    if ext is None:
        raise UploadRejected(UNSUPPORTED_MESSAGE)

    unique_filename = f"{uuid.uuid4()}{ext}"
    file_path = os.path.join(settings.UPLOAD_FOLDER, unique_filename)

    written = 0
    try:
        with open(file_path, "wb") as buffer:
            while chunk := file.file.read(CHUNK):
                written += len(chunk)
                if written > settings.MAX_UPLOAD_BYTES:
                    raise UploadRejected(
                        f"Ukuran file melebihi batas {settings.MAX_UPLOAD_MB} MB."
                    )
                buffer.write(chunk)
    except Exception:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise

    if written == 0:
        os.remove(file_path)
        raise UploadRejected("File kosong.")

    if ext == ".zip":
        kind = detect_office_kind(file_path)
        if kind is None:
            os.remove(file_path)
            raise UploadRejected(UNSUPPORTED_MESSAGE)
        final_path = os.path.splitext(file_path)[0] + kind
        os.replace(file_path, final_path)
        file_path, unique_filename = final_path, os.path.basename(final_path)

    return {
        "original_name": original_name,
        "saved_name": unique_filename,
        "size": written,
        "content_type": file.content_type,
    }


def _unique_name(name: str, used: set[str]) -> str:
    """'a.pdf' -> 'a.pdf', lalu 'a (2).pdf', 'a (3).pdf' ... supaya tidak saling menimpa di dalam ZIP."""
    stem, ext = os.path.splitext(name)
    candidate, n = name, 2
    while candidate.lower() in used:
        candidate = f"{stem} ({n}){ext}"
        n += 1
    used.add(candidate.lower())
    return candidate


def zip_results(items: list[tuple[str, str]]) -> Output:
    """Gabungkan file hasil jadi satu ZIP di folder results.
    items: [(path_file_hasil, nama_di_dalam_zip), ...]"""
    out = new_result(".zip")
    used: set[str] = set()
    try:
        # ZIP_STORED: hasil (JPG/PDF/DOCX) sudah terkompresi, jadi deflate tidak banyak membantu
        with zipfile.ZipFile(out.path, "w", zipfile.ZIP_STORED) as zf:
            for path, name in items:
                zf.write(path, _unique_name(name, used))
    except Exception:
        remove_file(out.path)
        raise
    return out


def remove_file(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def cleanup_old_files(max_age_seconds: float | None = None) -> int:
    """Hapus file upload & hasil yang lebih tua dari TTL. Return jumlah file terhapus."""
    if max_age_seconds is None:
        max_age_seconds = settings.FILE_TTL_MINUTES * 60
    cutoff = time.time() - max_age_seconds
    removed = 0
    for folder in (settings.UPLOAD_FOLDER, settings.RESULT_FOLDER):
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    return removed
