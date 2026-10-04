import os
import uuid
from dataclasses import dataclass
from app.core.config import settings


@dataclass
class Output:
    """Satu file hasil proses. `suffix` dipakai untuk menamai file saat di-download,
    mis. 'laporan.pdf' + '_compressed' -> 'laporan_compressed.pdf'."""
    name: str          # nama file di folder results (uuid + ext)
    path: str
    suffix: str = ""
    note: str | None = None   # info tambahan untuk user (mis. target ukuran tidak tercapai)


def new_result(ext: str, suffix: str = "") -> Output:
    name = f"{uuid.uuid4()}{ext}"
    return Output(name=name, path=os.path.join(settings.RESULT_FOLDER, name), suffix=suffix)
