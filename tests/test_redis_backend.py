import threading
import time

import fakeredis
import pytest
import redis
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core import ratelimit
from app.core.config import settings
from app.core.ratelimit import job_guard, limiter
from app.core.redis_backend import RedisBackend, manager
from app.main import app
from tests.test_api import download, png_bytes, process, upload

URL = "redis://fake:6379/0"


@pytest.fixture()
def server(monkeypatch):
    """Satu 'server Redis' palsu bersama; semua client yang dibuat manager terhubung ke sana."""
    srv = fakeredis.FakeServer()
    manager.set_client_factory(lambda url: fakeredis.FakeRedis(server=srv, decode_responses=True))
    monkeypatch.setattr(settings, "REDIS_URL", URL)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "REDIS_RETRY_SECONDS", 30)
    ratelimit.reset_all()
    yield srv
    ratelimit.reset_all()


def second_worker(srv) -> RedisBackend:
    """Client kedua ke server yang sama = worker/proses lain."""
    return RedisBackend(fakeredis.FakeRedis(server=srv, decode_responses=True))


# --------------------------------------------------------------- rate limit
def test_redis_is_selected_when_configured(server):
    assert manager.backend_name() == "redis"


def test_memory_is_used_when_redis_url_empty(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", "")
    assert manager.backend_name() == "memory"


def test_budget_then_wait_matches_memory_semantics(server):
    assert [limiter.hit("b", "1.1.1.1", 1, 3, 60) for _ in range(3)] == [None, None, None]
    wait = limiter.hit("b", "1.1.1.1", 1, 3, 60)
    assert wait is not None and 1 <= wait <= 60


def test_isolated_per_ip_and_bucket(server):
    assert limiter.hit("b", "A", 1, 1, 60) is None
    assert limiter.hit("b", "A", 1, 1, 60) is not None
    assert limiter.hit("b", "B", 1, 1, 60) is None
    assert limiter.hit("other", "A", 1, 1, 60) is None


def test_first_big_request_allowed_then_blocked(server):
    assert limiter.hit("b", "A", 10, 3, 60) is None
    assert limiter.hit("b", "A", 1, 3, 60) is not None


def test_fractional_costs_are_summed(server):
    # 0.4 + 0.4 = 0.8 <= 1.0 boleh; yang ketiga (1.2 > 1.0) harus ditolak
    assert limiter.hit("mb", "A", 0.4, 1.0, 60) is None
    assert limiter.hit("mb", "A", 0.4, 1.0, 60) is None
    assert limiter.hit("mb", "A", 0.4, 1.0, 60) is not None


def test_window_expires_and_key_is_cleaned_up(server):
    assert limiter.hit("w", "A", 1, 1, 1) is None
    assert limiter.hit("w", "A", 1, 1, 1) is not None
    time.sleep(1.2)
    assert limiter.hit("w", "A", 1, 1, 1) is None
    time.sleep(1.2)
    raw = fakeredis.FakeRedis(server=server, decode_responses=True)
    assert raw.keys("convertly:rl:w:*") == []          # PEXPIRE: tidak menumpuk selamanya


def test_wait_hint_tracks_when_budget_frees_up(server):
    """Biaya 2 + 1 dengan batas 3: untuk menambah biaya 2 perlu menunggu entri tertua keluar."""
    assert limiter.hit("t", "A", 2, 3, 60) is None
    assert limiter.hit("t", "A", 1, 3, 60) is None
    wait = limiter.hit("t", "A", 2, 3, 60)
    assert wait is not None and 1 <= wait <= 60


def test_counters_are_shared_between_workers(server):
    """Inti fitur ini: dua worker memakai SATU hitungan."""
    worker_a, worker_b = second_worker(server), second_worker(server)
    assert worker_a.hit("up", "9.9.9.9", 1, 3, 60) is None
    assert worker_b.hit("up", "9.9.9.9", 1, 3, 60) is None
    assert worker_a.hit("up", "9.9.9.9", 1, 3, 60) is None
    assert worker_b.hit("up", "9.9.9.9", 1, 3, 60) is not None     # jatah 3 habis lintas worker
    assert worker_a.hit("up", "9.9.9.9", 1, 3, 60) is not None


def test_keys_use_configured_prefix(server, monkeypatch):
    monkeypatch.setattr(settings, "REDIS_KEY_PREFIX", "appx")
    limiter.hit("b", "A", 1, 5, 60)
    raw = fakeredis.FakeRedis(server=server, decode_responses=True)
    assert raw.keys("appx:rl:b:A") == ["appx:rl:b:A"]


# ------------------------------------------------------------ job slots
def test_job_guard_per_ip_limit_via_redis(server, monkeypatch):
    monkeypatch.setattr(settings, "MAX_JOBS_PER_IP", 1)
    with job_guard.slot("1.2.3.4"):
        with pytest.raises(HTTPException) as err:
            with job_guard.slot("1.2.3.4"):
                pass
        assert err.value.status_code == 429
        with job_guard.slot("5.6.7.8"):
            pass
    with job_guard.slot("1.2.3.4"):                    # slot dikembalikan
        pass


def test_job_guard_global_limit_fails_fast_via_redis(server, monkeypatch):
    monkeypatch.setattr(settings, "MAX_CONCURRENT_JOBS", 1)
    monkeypatch.setattr(settings, "MAX_JOBS_PER_IP", 5)
    monkeypatch.setattr(settings, "JOB_QUEUE_TIMEOUT_SECONDS", 0)
    with job_guard.slot("A"):
        with pytest.raises(HTTPException) as err:
            with job_guard.slot("B"):
                pass
        assert err.value.status_code == 503
    with job_guard.slot("B"):
        pass
    raw = fakeredis.FakeRedis(server=server, decode_responses=True)
    assert raw.zcard("convertly:jobs:ip:B") == 0       # tiket IP B ikut dilepas saat gagal antre


def test_job_guard_releases_when_job_raises(server, monkeypatch):
    monkeypatch.setattr(settings, "MAX_JOBS_PER_IP", 1)
    with pytest.raises(RuntimeError):
        with job_guard.slot("A"):
            raise RuntimeError("proses gagal")
    with job_guard.slot("A"):
        pass


def test_job_slots_shared_between_workers(server, monkeypatch):
    monkeypatch.setattr(settings, "MAX_CONCURRENT_JOBS", 1)
    other = second_worker(server)
    assert other.enter("global", 1, "token-worker-lain") is True      # worker lain memegang satu-satunya slot
    monkeypatch.setattr(settings, "JOB_QUEUE_TIMEOUT_SECONDS", 0)
    with pytest.raises(HTTPException) as err:
        with job_guard.slot("A"):
            pass
    assert err.value.status_code == 503
    other.leave("global", "token-worker-lain")
    with job_guard.slot("A"):
        pass


def test_crashed_worker_slot_expires_by_lease(server, monkeypatch):
    """Worker yang crash tidak pernah memanggil leave(); sewa slot harus kedaluwarsa sendiri."""
    monkeypatch.setattr(settings, "JOB_LEASE_SECONDS", 1)
    monkeypatch.setattr(settings, "MAX_CONCURRENT_JOBS", 1)
    monkeypatch.setattr(settings, "JOB_QUEUE_TIMEOUT_SECONDS", 0)
    crashed = second_worker(server)
    assert crashed.enter("global", 1, "mati") is True
    with pytest.raises(HTTPException):
        with job_guard.slot("A"):
            pass
    time.sleep(1.3)
    with job_guard.slot("A"):                           # slot milik worker yang mati sudah bebas
        pass


def test_job_guard_thread_safe_via_redis(server, monkeypatch):
    monkeypatch.setattr(settings, "MAX_CONCURRENT_JOBS", 2)
    monkeypatch.setattr(settings, "MAX_JOBS_PER_IP", 100)
    monkeypatch.setattr(settings, "JOB_QUEUE_TIMEOUT_SECONDS", 10)
    active, peak, lock, errors = [0], [0], threading.Lock(), []

    def work(i):
        try:
            with job_guard.slot(f"ip{i}"):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.05)
                with lock:
                    active[0] -= 1
        except Exception as exc:     # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and peak[0] <= 2


# ----------------------------------------------------------------- fallback
def test_unreachable_redis_falls_back_to_memory_and_keeps_limiting(monkeypatch):
    def boom(url):
        raise redis.ConnectionError("connection refused")

    manager.set_client_factory(boom)
    monkeypatch.setattr(settings, "REDIS_URL", URL)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    ratelimit.reset_all()
    assert manager.backend_name() == "memory"
    assert limiter.hit("b", "A", 1, 1, 60) is None
    assert limiter.hit("b", "A", 1, 1, 60) is not None          # tetap dibatasi (per proses)
    with job_guard.slot("A"):
        pass


def test_redis_dying_midway_does_not_break_requests(server):
    assert limiter.hit("b", "A", 1, 5, 60) is None
    server.connected = False                                    # Redis tiba-tiba mati
    assert limiter.hit("b", "A", 1, 5, 60) is None              # tidak error: jatuh ke memori
    assert manager.backend_name() == "memory"
    with job_guard.slot("A"):
        pass


def test_manager_does_not_hammer_dead_redis_and_recovers(server, monkeypatch):
    monkeypatch.setattr(settings, "REDIS_RETRY_SECONDS", 1)
    manager.reset()                                             # lupakan koneksi yang sudah di-cache
    server.connected = False
    assert manager.get() is None                                # percobaan sambung pertama gagal
    server.connected = True
    assert manager.get() is None                                # masih masa tunggu: tidak mencoba lagi
    time.sleep(1.2)
    assert manager.get() is not None                            # pulih otomatis setelah masa tunggu


# -------------------------------------------------------- end-to-end via API
@pytest.fixture()
def api(tmp_path, server, monkeypatch):
    up, res = tmp_path / "uploads", tmp_path / "results"
    up.mkdir()
    res.mkdir()
    monkeypatch.setattr(settings, "UPLOAD_FOLDER", str(up))
    monkeypatch.setattr(settings, "RESULT_FOLDER", str(res))
    with TestClient(app) as c:
        yield c


def post_png(client):
    return client.post("/api/v1/upload", files=[("files", ("a.png", png_bytes(), "image/png"))])


def test_api_upload_limit_is_enforced_through_redis(api, server, monkeypatch):
    monkeypatch.setattr(settings, "RATE_UPLOAD_PER_MIN", 2)
    assert [post_png(api).status_code for _ in range(3)] == [200, 200, 429]
    raw = fakeredis.FakeRedis(server=server, decode_responses=True)
    assert raw.keys("convertly:rl:upload:*")                    # hitungannya memang ada di Redis


def test_api_process_and_download_limits_through_redis(api, monkeypatch):
    monkeypatch.setattr(settings, "RATE_PROCESS_COST_PER_MIN", 2)
    monkeypatch.setattr(settings, "RATE_DOWNLOAD_PER_MIN", 1)
    names = [upload(api, f"{i}.png", png_bytes()) for i in range(3)]
    ok = [process(api, "convert", [n], format="jpg") for n in names[:2]]
    assert [r.status_code for r in ok] == [200, 200]
    assert process(api, "convert", [names[2]], format="jpg").status_code == 429
    url = f"/api/v1/download/{ok[0].json()['results'][0]['processed_file']}"
    assert api.get(url).status_code == 200
    assert api.get(url).status_code == 429


def test_api_keeps_working_when_redis_dies(api, server, monkeypatch):
    monkeypatch.setattr(settings, "RATE_UPLOAD_PER_MIN", 100)
    assert post_png(api).status_code == 200
    server.connected = False
    assert post_png(api).status_code == 200                     # layanan tidak ikut mati
    n = upload(api, "b.png", png_bytes())
    r = process(api, "convert", [n], format="jpg")
    assert r.status_code == 200 and download(api, r)
