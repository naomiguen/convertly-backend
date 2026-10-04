"""Pengaman penyalahgunaan: rate limit per IP, batas pekerjaan serentak, dan kuota penyimpanan.

Penghitung disimpan di Redis jika REDIS_URL diisi (dibagi semua worker/instance). Tanpa Redis, atau saat Redis
sedang tidak terjangkau, dipakai penghitung memori proses: tiap worker punya hitungan sendiri, jadi batas
efektifnya dikali jumlah worker, tetapi API tetap berjalan dan tetap terlindungi.
"""
import math
import os
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager

from fastapi import HTTPException, Request

from app.core.config import settings
from app.core.redis_backend import RedisUnavailable, manager as redis_manager

# Bobot biaya tiap aksi pada /process (dikalikan jumlah file untuk aksi per-file)
HEAVY_ACTIONS = {"ocr-pdf", "ocr-text", "docx-to-pdf", "pptx-to-pdf"}
MEDIUM_ACTIONS = {"pdf-to-docx", "id-photo", "compress-pdf", "pdf-to-image"}
COST = {"heavy": 4, "medium": 2, "light": 1}
# Aksi yang memakai banyak CPU: jumlah file per request dibatasi MAX_HEAVY_FILES_PER_REQUEST
FILE_CAPPED_ACTIONS = HEAVY_ACTIONS | {"pdf-to-docx", "id-photo"}


def action_cost(action: str) -> int:
    if action in HEAVY_ACTIONS:
        return COST["heavy"]
    if action in MEDIUM_ACTIONS:
        return COST["medium"]
    return COST["light"]


def client_ip(request: Request) -> str:
    """IP klien. X-Forwarded-For hanya dipercaya jika TRUSTED_PROXY_COUNT > 0 (diambil dari kanan,
    yaitu entri yang ditambahkan proxy tepercaya kita, bukan yang bisa diisi klien)."""
    n = settings.TRUSTED_PROXY_COUNT
    if n > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if len(parts) >= n:
            return parts[-n]
    return request.client.host if request.client else "unknown"


def too_many(retry_after: int, message: str, status: int = 429) -> HTTPException:
    return HTTPException(status_code=status, detail=message, headers={"Retry-After": str(max(1, retry_after))})


class SlidingWindowLimiter:
    """Anggaran 'biaya' dalam jendela waktu geser per (bucket, ip).
    Aturan penting: request pertama pada jendela kosong selalu boleh, sebesar apa pun biayanya,
    supaya satu pekerjaan sah yang besar tidak mustahil dijalankan; request berikutnya baru dibatasi."""

    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[tuple[float, float]]] = {}
        self._lock = threading.Lock()

    def hit(self, bucket: str, ip: str, cost: float, limit: float, window: float) -> int | None:
        """None jika diizinkan (dan dicatat); jika ditolak, return detik yang perlu ditunggu."""
        now = time.monotonic()
        with self._lock:
            events = self._events.setdefault((bucket, ip), deque())
            while events and events[0][0] <= now - window:
                events.popleft()
            used = sum(c for _, c in events)
            if events and used + cost > limit:
                need, freed = used + cost - limit, 0.0
                for t, c in events:
                    freed += c
                    if freed >= need:
                        return math.ceil(t + window - now)
                return math.ceil(window)
            events.append((now, cost))
            return None

    def purge(self, max_age: float = 3600.0) -> None:
        """Buang entri lama agar memori tidak membengkak oleh banyak IP berbeda."""
        cutoff = time.monotonic() - max_age
        with self._lock:
            for key in [k for k, ev in self._events.items() if not ev or ev[-1][0] < cutoff]:
                del self._events[key]

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


class Limiter:
    """Fasad: pakai Redis bila tersedia (hitungan bersama), jika tidak penghitung memori."""

    def __init__(self) -> None:
        self.memory = SlidingWindowLimiter()

    def hit(self, bucket: str, ip: str, cost: float, limit: float, window: float) -> int | None:
        backend = redis_manager.get()
        if backend is not None:
            try:
                return backend.hit(bucket, ip, cost, limit, window)
            except RedisUnavailable:
                pass   # Redis baru saja gagal: lanjut ke penghitung memori
        return self.memory.hit(bucket, ip, cost, limit, window)

    def purge(self) -> None:
        self.memory.purge()   # kunci Redis kedaluwarsa sendiri (PEXPIRE)

    def reset(self) -> None:
        self.memory.reset()


limiter = Limiter()


def check_rate(request: Request, bucket: str, cost: float, limit: float, window: float, what: str) -> str:
    """Terapkan rate limit untuk request ini. Return IP klien."""
    ip = client_ip(request)
    if settings.RATE_LIMIT_ENABLED:
        wait = limiter.hit(bucket, ip, cost, limit, window)
        if wait is not None:
            raise too_many(wait, f"Terlalu banyak {what}. Coba lagi dalam {wait} detik.")
    return ip


def check_upload_rate(request: Request) -> str:
    return check_rate(request, "upload", 1, settings.RATE_UPLOAD_PER_MIN, 60, "upload")


def check_download_rate(request: Request) -> str:
    return check_rate(request, "download", 1, settings.RATE_DOWNLOAD_PER_MIN, 60, "unduhan")


def check_process_rate(request: Request, cost: float) -> str:
    return check_rate(request, "process", cost, settings.RATE_PROCESS_COST_PER_MIN, 60, "permintaan proses")


def check_upload_volume(ip: str, megabytes: float) -> None:
    """Kuota volume upload per IP per jam. Dipanggil setelah file tersimpan; pemanggil menghapus file jika ditolak."""
    if not settings.RATE_LIMIT_ENABLED:
        return
    wait = limiter.hit("upload-mb", ip, megabytes, settings.UPLOAD_MB_PER_HOUR, 3600)
    if wait is not None:
        minutes = max(1, math.ceil(wait / 60))
        raise too_many(wait, f"Kuota upload per jam habis. Coba lagi dalam sekitar {minutes} menit.")


class JobGuard:
    """Membatasi pekerjaan berat yang berjalan bersamaan: per IP dan global.
    Jika semua slot global terpakai, request menunggu sebentar lalu gagal cepat dengan 503
    (daripada menumpuk tanpa batas dan menghabiskan thread server)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._per_ip: dict[str, int] = {}
        self._semaphore: threading.BoundedSemaphore | None = None
        self._size = 0

    def _get_semaphore(self) -> threading.BoundedSemaphore:
        with self._lock:
            if self._semaphore is None or self._size != settings.MAX_CONCURRENT_JOBS:
                self._size = settings.MAX_CONCURRENT_JOBS
                self._semaphore = threading.BoundedSemaphore(self._size)
            return self._semaphore

    @contextmanager
    def slot(self, ip: str):
        if not settings.RATE_LIMIT_ENABLED:
            yield
            return
        backend = redis_manager.get()
        token = None
        if backend is not None:
            try:
                token = self._acquire_redis(backend, ip)
            except RedisUnavailable:
                backend = None   # Redis gagal saat mengambil slot: pakai penghitung memori
        if backend is not None:
            try:
                yield
            finally:
                backend.leave(f"ip:{ip}", token)
                backend.leave("global", token)
            return
        with self._memory_slot(ip):
            yield

    def _acquire_redis(self, backend, ip: str) -> str:
        """Slot per IP dulu (menghitung yang menunggu juga), lalu tunggu slot global dengan polling."""
        token = uuid.uuid4().hex
        if not backend.enter(f"ip:{ip}", settings.MAX_JOBS_PER_IP, token):
            raise too_many(5, "Masih ada proses yang berjalan dari perangkat Anda. Tunggu sampai selesai.")
        deadline = time.monotonic() + settings.JOB_QUEUE_TIMEOUT_SECONDS
        try:
            while not backend.enter("global", settings.MAX_CONCURRENT_JOBS, token):
                if time.monotonic() >= deadline:
                    raise too_many(10, "Server sedang sibuk. Coba lagi sebentar lagi.", status=503)
                time.sleep(0.15)
        except BaseException:
            backend.leave(f"ip:{ip}", token)
            raise
        return token

    @contextmanager
    def _memory_slot(self, ip: str):
        with self._lock:
            if self._per_ip.get(ip, 0) >= settings.MAX_JOBS_PER_IP:
                raise too_many(5, "Masih ada proses yang berjalan dari perangkat Anda. Tunggu sampai selesai.")
            self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
        semaphore = self._get_semaphore()
        acquired = semaphore.acquire(timeout=settings.JOB_QUEUE_TIMEOUT_SECONDS)
        try:
            if not acquired:
                raise too_many(10, "Server sedang sibuk. Coba lagi sebentar lagi.", status=503)
            try:
                yield
            finally:
                semaphore.release()
        finally:
            with self._lock:
                self._per_ip[ip] -= 1
                if self._per_ip[ip] <= 0:
                    del self._per_ip[ip]

    def reset(self) -> None:
        with self._lock:
            self._per_ip.clear()
            self._semaphore = None


job_guard = JobGuard()

# ---- Kuota penyimpanan (upload + hasil) ----
_storage_cache: tuple[float, int] = (0.0, 0)
_storage_lock = threading.Lock()


def storage_used_bytes(max_age: float = 5.0) -> int:
    """Total ukuran isi folder upload + hasil (di-cache beberapa detik)."""
    global _storage_cache
    with _storage_lock:
        now = time.monotonic()
        if now - _storage_cache[0] < max_age:
            return _storage_cache[1]
        total = 0
        for folder in (settings.UPLOAD_FOLDER, settings.RESULT_FOLDER):
            if os.path.isdir(folder):
                with os.scandir(folder) as entries:
                    for entry in entries:
                        try:
                            if entry.is_file():
                                total += entry.stat().st_size
                        except OSError:
                            pass
        _storage_cache = (now, total)
        return total


def check_storage_available() -> None:
    if not settings.RATE_LIMIT_ENABLED:
        return
    if storage_used_bytes() >= settings.MAX_STORAGE_MB * 1024 * 1024:
        raise too_many(60, "Penyimpanan server sedang penuh. Coba lagi beberapa menit lagi.", status=503)


def reset_all() -> None:
    """Dipakai tes: kosongkan semua state (memori, dan kunci Redis jika sedang terhubung)."""
    global _storage_cache
    limiter.reset()
    job_guard.reset()
    _storage_cache = (0.0, 0)
    backend = redis_manager.get()
    if backend is not None:
        try:
            backend.flush()
        except Exception:
            pass
