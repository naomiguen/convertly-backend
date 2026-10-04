"""Tes terhadap server Redis SUNGGUHAN. Dilewati kecuali TEST_REDIS_URL diisi, mis.:

    docker run -d --name convertly-test-redis -p 6390:6379 redis:7-alpine
    TEST_REDIS_URL=redis://127.0.0.1:6390/0 pytest tests/test_redis_real.py

Tes lain memakai fakeredis; file ini memastikan skrip Lua dan perilaku atomik benar di Redis asli.
"""
import os
import threading
import time
import uuid

import pytest
import redis
from fastapi import HTTPException

from app.core import ratelimit
from app.core.config import settings
from app.core.ratelimit import job_guard, limiter
from app.core.redis_backend import manager

REAL_URL = os.environ.get("TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not REAL_URL, reason="TEST_REDIS_URL tidak diisi (butuh Redis sungguhan)")


@pytest.fixture()
def real(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", REAL_URL)
    monkeypatch.setattr(settings, "REDIS_KEY_PREFIX", f"convertly-test-{uuid.uuid4().hex[:8]}")   # tidak mengganggu data lain
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    manager.reset()
    ratelimit.reset_all()
    assert manager.backend_name() == "redis"
    yield redis.Redis.from_url(REAL_URL, decode_responses=True)
    ratelimit.reset_all()


def test_server_is_real_redis(real):
    info = real.info("server")
    assert "redis_version" in info and "fake" not in str(info).lower()


def test_budget_wait_isolation_and_first_big(real):
    assert [limiter.hit("b", "1.1.1.1", 1, 3, 60) for _ in range(3)] == [None, None, None]
    wait = limiter.hit("b", "1.1.1.1", 1, 3, 60)
    assert wait is not None and 1 <= wait <= 60
    assert limiter.hit("b", "2.2.2.2", 1, 3, 60) is None             # IP lain
    assert limiter.hit("other", "1.1.1.1", 1, 3, 60) is None         # bucket lain
    assert limiter.hit("big", "A", 10, 3, 60) is None                # request besar pertama boleh
    assert limiter.hit("big", "A", 1, 3, 60) is not None


def test_fractional_costs(real):
    assert limiter.hit("mb", "A", 0.4, 1.0, 60) is None
    assert limiter.hit("mb", "A", 0.4, 1.0, 60) is None
    assert limiter.hit("mb", "A", 0.4, 1.0, 60) is not None


def test_window_expiry_and_key_ttl(real):
    key = f"{settings.REDIS_KEY_PREFIX}:rl:w:A"
    assert limiter.hit("w", "A", 1, 1, 2) is None
    ttl = real.pttl(key)
    assert 0 < ttl <= 2000                                           # PEXPIRE terpasang
    assert limiter.hit("w", "A", 1, 1, 2) is not None
    time.sleep(2.3)
    assert real.exists(key) == 0                                     # kunci hilang sendiri
    assert limiter.hit("w", "A", 1, 1, 2) is None


def test_wait_hint_is_accurate(real):
    assert limiter.hit("t", "A", 1, 1, 5) is None
    wait = limiter.hit("t", "A", 1, 1, 5)
    assert wait in (4, 5)                                            # ~sisa jendela 5 detik


def test_limit_is_exact_under_concurrency(real):
    """Bukti atomisitas: 60 thread berebut jatah 10 -> tepat 10 yang lolos (tanpa race)."""
    allowed, lock = [], threading.Lock()
    barrier = threading.Barrier(60)

    def go():
        barrier.wait()
        ok = limiter.hit("race", "same-ip", 1, 10, 60) is None
        with lock:
            allowed.append(ok)

    threads = [threading.Thread(target=go) for _ in range(60)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(allowed) == 10


def test_counters_shared_across_independent_connections(real):
    """Dua manager/koneksi terpisah (seperti dua worker) memakai hitungan yang sama."""
    from app.core.redis_backend import RedisBackend
    other = RedisBackend(redis.Redis.from_url(REAL_URL, decode_responses=True))
    assert limiter.hit("up", "9.9.9.9", 1, 3, 60) is None
    assert other.hit("up", "9.9.9.9", 1, 3, 60) is None
    assert limiter.hit("up", "9.9.9.9", 1, 3, 60) is None
    assert other.hit("up", "9.9.9.9", 1, 3, 60) is not None


def test_script_cache_flush_is_recovered_automatically(real):
    """Redis restart / SCRIPT FLUSH menghapus cache skrip: redis-py harus memuat ulang tanpa error."""
    assert limiter.hit("s", "A", 1, 5, 60) is None
    real.script_flush()
    assert limiter.hit("s", "A", 1, 5, 60) is None
    assert manager.backend_name() == "redis"                         # tidak dianggap mati
    real.script_flush()
    with job_guard.slot("A"):
        pass


def test_job_slots_per_ip_global_and_release(real, monkeypatch):
    monkeypatch.setattr(settings, "MAX_JOBS_PER_IP", 1)
    monkeypatch.setattr(settings, "MAX_CONCURRENT_JOBS", 1)
    monkeypatch.setattr(settings, "JOB_QUEUE_TIMEOUT_SECONDS", 0)
    with job_guard.slot("1.2.3.4"):
        with pytest.raises(HTTPException) as per_ip:
            with job_guard.slot("1.2.3.4"):
                pass
        assert per_ip.value.status_code == 429
        with pytest.raises(HTTPException) as full:
            with job_guard.slot("5.6.7.8"):
                pass
        assert full.value.status_code == 503
        assert real.zcard(f"{settings.REDIS_KEY_PREFIX}:jobs:ip:5.6.7.8") == 0     # tiket IP yang gagal antre dilepas
    with job_guard.slot("1.2.3.4"):
        pass
    assert real.zcard(f"{settings.REDIS_KEY_PREFIX}:jobs:global") == 0


def test_crashed_worker_slot_expires_by_lease(real, monkeypatch):
    from app.core.redis_backend import RedisBackend
    monkeypatch.setattr(settings, "JOB_LEASE_SECONDS", 1)
    monkeypatch.setattr(settings, "MAX_CONCURRENT_JOBS", 1)
    monkeypatch.setattr(settings, "JOB_QUEUE_TIMEOUT_SECONDS", 0)
    dead = RedisBackend(redis.Redis.from_url(REAL_URL, decode_responses=True))
    assert dead.enter("global", 1, "mati") is True                   # tidak pernah leave()
    with pytest.raises(HTTPException):
        with job_guard.slot("A"):
            pass
    time.sleep(2.2)                                                  # TIME Redis: sewa 1 detik sudah lewat
    with job_guard.slot("A"):
        pass


def test_global_concurrency_never_exceeds_limit(real, monkeypatch):
    monkeypatch.setattr(settings, "MAX_CONCURRENT_JOBS", 2)
    monkeypatch.setattr(settings, "MAX_JOBS_PER_IP", 100)
    monkeypatch.setattr(settings, "JOB_QUEUE_TIMEOUT_SECONDS", 15)
    active, peak, lock, errors = [0], [0], threading.Lock(), []

    def work(i):
        try:
            with job_guard.slot(f"ip{i}"):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.1)
                with lock:
                    active[0] -= 1
        except Exception as exc:     # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and peak[0] <= 2
