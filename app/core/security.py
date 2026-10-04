import os
import re

from app.core.config import settings

# Nama file internal selalu "<uuid4>.<ext>" (dibuat oleh server). Client hanya boleh
# mengirim nama yang cocok dengan pola ini, jadi ../ atau \ tidak mungkin lolos.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,100}\.[A-Za-z0-9]{1,5}$")


def is_safe_filename(name: str) -> bool:
    return bool(name) and bool(_SAFE_NAME.match(name))


def safe_path(folder: str, name: str) -> str:
    """Gabungkan folder + nama file, tolak jika nama tidak aman / keluar dari folder."""
    if not is_safe_filename(name):
        raise ValueError("Nama file tidak valid.")
    folder_abs = os.path.abspath(folder)
    full = os.path.abspath(os.path.join(folder_abs, name))
    if os.path.commonpath([folder_abs, full]) != folder_abs:
        raise ValueError("Nama file tidak valid.")
    return full


def upload_path(name: str) -> str:
    return safe_path(settings.UPLOAD_FOLDER, name)


def result_path(name: str) -> str:
    return safe_path(settings.RESULT_FOLDER, name)


def clean_download_name(name: str | None, fallback: str) -> str:
    """Nama tampilan saat download (dari nama asli file mahasiswa). Dibersihkan agar
    aman di header Content-Disposition dan di Windows."""
    if not name:
        return fallback
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:150] or fallback
