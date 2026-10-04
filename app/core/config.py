import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parents[2]  # folder backend/


def _resolve_dir(value: str) -> str:
    """Path relatif di .env dihitung dari folder backend/, bukan dari working directory,
    supaya server bisa dijalankan dari mana saja."""
    path = Path(value)
    if not path.is_absolute():
        path = BASE_DIR / path
    return str(path.resolve())


class Settings:
    PROJECT_NAME: str = os.getenv("PROJECT_NAME", "Convertly API")

    UPLOAD_FOLDER: str = _resolve_dir(os.getenv("UPLOAD_FOLDER", "../storage/uploads"))
    RESULT_FOLDER: str = _resolve_dir(os.getenv("RESULT_FOLDER", "../storage/results"))

    # Batas upload per file (MB) dan jumlah file per request
    MAX_UPLOAD_MB: int = int(os.getenv("MAX_UPLOAD_MB", "50"))
    MAX_FILES_PER_UPLOAD: int = int(os.getenv("MAX_FILES_PER_UPLOAD", "20"))

    # OCR berat di CPU: batasi halaman (yang perlu di-OCR) per file
    MAX_OCR_PAGES: int = int(os.getenv("MAX_OCR_PAGES", "20"))

    # File upload/hasil dihapus otomatis setelah sekian menit
    FILE_TTL_MINUTES: int = int(os.getenv("FILE_TTL_MINUTES", "30"))
    CLEANUP_INTERVAL_MINUTES: int = int(os.getenv("CLEANUP_INTERVAL_MINUTES", "5"))

    # Pisahkan dengan koma, contoh: http://localhost:5173,https://convertly.example.com
    CORS_ORIGINS: list[str] = [
        o.strip()
        for o in os.getenv(
            "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
        if o.strip()
    ]

    # Redis: penyimpan bersama untuk rate limit & batas pekerjaan serentak, supaya hitungannya konsisten
    # walau backend dijalankan dengan banyak worker/instance. Kosong = pakai memori proses.
    # Jika Redis tidak terjangkau, otomatis kembali ke memori (API tetap hidup) dan dicoba lagi berkala.
    REDIS_URL: str = os.getenv("REDIS_URL", "")
    REDIS_KEY_PREFIX: str = os.getenv("REDIS_KEY_PREFIX", "convertly")
    REDIS_TIMEOUT_SECONDS: float = float(os.getenv("REDIS_TIMEOUT_SECONDS", "0.5"))
    REDIS_RETRY_SECONDS: int = int(os.getenv("REDIS_RETRY_SECONDS", "30"))
    # Batas umur "sewa" slot pekerjaan di Redis: slot milik worker yang crash bebas sendiri setelah ini
    JOB_LEASE_SECONDS: int = int(os.getenv("JOB_LEASE_SECONDS", "1800"))

    # ---- Pengaman penyalahgunaan (per IP, disimpan di memori proses) ----
    RATE_LIMIT_ENABLED: bool = os.getenv("RATE_LIMIT_ENABLED", "true").lower() in ("1", "true", "yes")
    # Di belakang reverse proxy (nginx/Cloudflare) isi jumlah proxy tepercaya agar IP asli dibaca
    # dari X-Forwarded-For. Biarkan 0 jika server diakses langsung (header itu bisa dipalsukan).
    TRUSTED_PROXY_COUNT: int = int(os.getenv("TRUSTED_PROXY_COUNT", "0"))
    RATE_UPLOAD_PER_MIN: int = int(os.getenv("RATE_UPLOAD_PER_MIN", "20"))              # request upload / menit
    RATE_PROCESS_COST_PER_MIN: int = int(os.getenv("RATE_PROCESS_COST_PER_MIN", "30"))  # poin biaya / menit
    RATE_DOWNLOAD_PER_MIN: int = int(os.getenv("RATE_DOWNLOAD_PER_MIN", "120"))         # unduhan / menit
    UPLOAD_MB_PER_HOUR: float = float(os.getenv("UPLOAD_MB_PER_HOUR", "1000"))          # volume upload / jam
    MAX_REQUEST_MB: float = float(os.getenv("MAX_REQUEST_MB", "300"))                   # ukuran total 1 request upload
    MAX_HEAVY_FILES_PER_REQUEST: int = int(os.getenv("MAX_HEAVY_FILES_PER_REQUEST", "5"))  # OCR, Office->PDF, dll.
    MAX_CONCURRENT_JOBS: int = int(os.getenv("MAX_CONCURRENT_JOBS", "4"))               # pekerjaan serentak (global)
    MAX_JOBS_PER_IP: int = int(os.getenv("MAX_JOBS_PER_IP", "2"))                       # pekerjaan serentak per IP
    JOB_QUEUE_TIMEOUT_SECONDS: int = int(os.getenv("JOB_QUEUE_TIMEOUT_SECONDS", "30"))  # tunggu antrean sebelum 503
    MAX_STORAGE_MB: float = float(os.getenv("MAX_STORAGE_MB", "5000"))                  # total isi folder upload+hasil

    @property
    def MAX_UPLOAD_BYTES(self) -> int:
        return self.MAX_UPLOAD_MB * 1024 * 1024


settings = Settings()
