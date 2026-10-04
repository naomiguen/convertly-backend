import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core import ratelimit
from app.core.config import settings
from app.core.ratelimit import SlidingWindowLimiter, action_cost, job_guard
from app.main import app
from tests.test_api import download, png_bytes, process, upload


@pytest.fixture()
def guarded(tmp_path, monkeypatch):
    """Client dengan pengaman AKTIF dan state bersih."""
    up, res = tmp_path / "uploads", tmp_path / "results"
    up.mkdir()
    res.mkdir()
    monkeypatch.setattr(settings, "UPLOAD_FOLDER", str(up))
    monkeypatch.setattr(settings, "RESULT_FOLDER", str(res))
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    ratelimit.reset_all()
    with TestClient(app) as c:
        yield c
    ratelimit.reset_all()


def post_png(client, name="a.png", data=None, headers=None):
    data = data or png_bytes()
    return client.post("/api/v1/upload", files=[("files", (name, data, "image/png"))], headers=headers or {})


# ------------------------------------------------------------ limiter (unit)
def test_limiter_allows_up_to_budget_then_reports_wait():
    lim = SlidingWindowLimiter()
    assert [lim.hit("b", "1.1.1.1", 1, 3, 60) for _ in range(3)] == [None, None, None]
    wait = lim.hit("b", "1.1.1.1", 1, 3, 60)
    assert wait is not None and 1 <= wait <= 60


def test_limiter_is_per_ip_and_per_bucket():
    lim = SlidingWindowLimiter()
    assert lim.hit("b", "A", 1, 1, 60) is None
    assert lim.hit("b", "A", 1, 1, 60) is not None
    assert lim.hit("b", "B", 1, 1, 60) is None          # IP lain tidak terpengaruh
    assert lim.hit("other", "A", 1, 1, 60) is None      # bucket lain tidak terpengaruh


def test_limiter_first_request_may_exceed_budget_but_next_is_blocked():
    lim = SlidingWindowLimiter()
    assert lim.hit("b", "A", 10, 3, 60) is None         # pekerjaan sah yang besar tetap boleh jalan
    assert lim.hit("b", "A", 1, 3, 60) is not None


def test_limiter_window_expires(monkeypatch):
    lim = SlidingWindowLimiter()
    clock = [1000.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: clock[0])
    assert lim.hit("b", "A", 1, 1, 60) is None
    assert lim.hit("b", "A", 1, 1, 60) is not None
    clock[0] += 61
    assert lim.hit("b", "A", 1, 1, 60) is None


def test_limiter_purge_drops_stale_keys(monkeypatch):
    lim = SlidingWindowLimiter()
    clock = [0.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: clock[0])
    lim.hit("b", "A", 1, 5, 60)
    clock[0] = 7200
    lim.purge()
    assert lim._events == {}


def test_action_costs_rank_heavy_above_light():
    assert action_cost("ocr-pdf") > action_cost("compress-pdf") > action_cost("rotate")


# ------------------------------------------------------------------- upload
def test_upload_rate_limit_returns_429_with_retry_after(guarded, monkeypatch):
    monkeypatch.setattr(settings, "RATE_UPLOAD_PER_MIN", 3)
    assert [post_png(guarded).status_code for _ in range(3)] == [200, 200, 200]
    r = post_png(guarded)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1 and "Terlalu banyak upload" in r.json()["detail"]


def test_rate_limit_response_still_has_cors_headers(guarded, monkeypatch):
    monkeypatch.setattr(settings, "RATE_UPLOAD_PER_MIN", 1)
    origin = {"Origin": "http://localhost:5173"}
    post_png(guarded, headers=origin)
    r = post_png(guarded, headers=origin)
    assert r.status_code == 429
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"   # browser bisa membaca pesannya
    assert "retry-after" in r.headers["access-control-expose-headers"].lower()


def test_rate_limit_can_be_disabled(guarded, monkeypatch):
    monkeypatch.setattr(settings, "RATE_UPLOAD_PER_MIN", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    assert [post_png(guarded).status_code for _ in range(4)] == [200] * 4


def test_oversized_request_rejected_before_body_is_read(guarded, monkeypatch):
    monkeypatch.setattr(settings, "MAX_REQUEST_MB", 0.01)       # ~10 KB
    r = post_png(guarded, data=png_bytes(size=(900, 900)) + b"0" * 50_000)
    assert r.status_code == 413 and "melebihi batas" in r.json()["detail"]


def test_total_size_checked_even_without_content_length(guarded, monkeypatch):
    """Body chunked tidak punya Content-Length, jadi middleware tidak bisa menolaknya lebih awal;
    ukuran sebenarnya harus dicek lagi setelah file tersimpan, dan file yang ditolak dihapus."""
    import os
    monkeypatch.setattr(settings, "MAX_REQUEST_MB", 0.01)   # ~10 KB
    boundary = "xBOUNDARYx"
    data = png_bytes(size=(900, 900)) + b"0" * 30_000
    crlf = b"\r\n"
    head = (f"--{boundary}".encode() + crlf
            + b'Content-Disposition: form-data; name="files"; filename="a.png"' + crlf
            + b"Content-Type: image/png" + crlf + crlf)
    tail = crlf + f"--{boundary}--".encode() + crlf
    r = guarded.post(
        "/api/v1/upload", content=iter([head, data, tail]),      # iterator -> Transfer-Encoding: chunked
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert "content-length" not in r.request.headers
    assert r.status_code == 413 and "Total ukuran file melebihi" in r.json()["detail"]
    assert os.listdir(settings.UPLOAD_FOLDER) == []


def test_hourly_upload_volume_quota_removes_rejected_files(guarded, monkeypatch):
    import os
    monkeypatch.setattr(settings, "UPLOAD_MB_PER_HOUR", 0.01)
    big = png_bytes(size=(900, 900)) + b"0" * 20_000
    assert post_png(guarded, data=big).status_code == 200       # jendela kosong: boleh
    r = post_png(guarded, data=big)
    assert r.status_code == 429 and "Kuota upload per jam" in r.json()["detail"]
    assert len(os.listdir(settings.UPLOAD_FOLDER)) == 1         # file dari upload yang ditolak ikut dihapus


def test_storage_cap_returns_503(guarded, monkeypatch):
    monkeypatch.setattr(settings, "MAX_STORAGE_MB", 0.001)       # ~1 KB
    filler = png_bytes() + b"0" * 5_000                          # pasti melewati batas setelah tersimpan
    assert post_png(guarded, data=filler).status_code == 200     # penyimpanan masih kosong saat upload pertama
    ratelimit.reset_all()                                       # abaikan cache ukuran
    r = post_png(guarded)
    assert r.status_code == 503 and "penuh" in r.json()["detail"]


# ------------------------------------------------------------------ process
def test_process_cost_budget(guarded, monkeypatch):
    monkeypatch.setattr(settings, "RATE_PROCESS_COST_PER_MIN", 3)
    names = [upload(guarded, f"{i}.png", png_bytes()) for i in range(5)]
    assert [process(guarded, "convert", [n], format="jpg").status_code for n in names[:3]] == [200, 200, 200]
    r = process(guarded, "convert", [names[3]], format="jpg")
    assert r.status_code == 429 and "Terlalu banyak permintaan proses" in r.json()["detail"]
    # file upload TIDAK terhapus saat ditolak, jadi bisa dicoba lagi tanpa upload ulang
    assert process(guarded, "convert", [names[3]], format="jpg").status_code == 429


def test_one_big_batch_is_allowed_then_next_request_waits(guarded, monkeypatch):
    monkeypatch.setattr(settings, "RATE_PROCESS_COST_PER_MIN", 2)
    names = [upload(guarded, f"{i}.png", png_bytes()) for i in range(5)]
    assert process(guarded, "convert", names[:4], format="jpg").status_code == 200   # biaya 4 > 2, tapi jendela kosong
    assert process(guarded, "convert", [names[4]], format="jpg").status_code == 429


def test_heavy_actions_limited_per_request(guarded, monkeypatch):
    monkeypatch.setattr(settings, "MAX_HEAVY_FILES_PER_REQUEST", 2)
    names = [upload(guarded, f"{i}.png", png_bytes()) for i in range(3)]
    r = process(guarded, "ocr-text", names)
    assert r.status_code == 400 and "maksimal 2 file" in r.json()["detail"]
    # alat ringan tidak dibatasi jumlah file-nya
    assert process(guarded, "convert", names, format="jpg").status_code == 200


# ------------------------------------------------------------- job concurrency
def test_job_guard_limits_concurrent_jobs_per_ip(monkeypatch):
    ratelimit.reset_all()
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "MAX_JOBS_PER_IP", 1)
    with job_guard.slot("1.2.3.4"):
        with pytest.raises(HTTPException) as err:
            with job_guard.slot("1.2.3.4"):
                pass
        assert err.value.status_code == 429
        with job_guard.slot("5.6.7.8"):          # IP lain tetap boleh
            pass
    with job_guard.slot("1.2.3.4"):              # slot dikembalikan setelah selesai
        pass


def test_job_guard_global_limit_fails_fast_with_503(monkeypatch):
    ratelimit.reset_all()
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "MAX_CONCURRENT_JOBS", 1)
    monkeypatch.setattr(settings, "MAX_JOBS_PER_IP", 5)
    monkeypatch.setattr(settings, "JOB_QUEUE_TIMEOUT_SECONDS", 0)
    with job_guard.slot("A"):
        with pytest.raises(HTTPException) as err:
            with job_guard.slot("B"):
                pass
        assert err.value.status_code == 503 and "sibuk" in err.value.detail
    with job_guard.slot("B"):                    # kosong lagi
        pass


def test_job_guard_releases_slot_when_job_raises(monkeypatch):
    ratelimit.reset_all()
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "MAX_JOBS_PER_IP", 1)
    with pytest.raises(RuntimeError):
        with job_guard.slot("A"):
            raise RuntimeError("proses gagal")
    with job_guard.slot("A"):
        pass


def test_job_guard_is_thread_safe(monkeypatch):
    ratelimit.reset_all()
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
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
                threading.Event().wait(0.05)
                with lock:
                    active[0] -= 1
        except Exception as exc:       # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and peak[0] <= 2


# ----------------------------------------------------------------- download
def test_download_rate_limit_and_private_headers(guarded, monkeypatch):
    monkeypatch.setattr(settings, "RATE_DOWNLOAD_PER_MIN", 2)
    n = upload(guarded, "a.png", png_bytes())
    res = process(guarded, "convert", [n], format="jpg").json()["results"][0]
    url = f"/api/v1/download/{res['processed_file']}"
    first = guarded.get(url)
    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, no-store"
    assert first.headers["x-content-type-options"] == "nosniff"
    assert guarded.get(url).status_code == 200
    r = guarded.get(url)
    assert r.status_code == 429 and "unduhan" in r.json()["detail"]


# ------------------------------------------------------------------ client IP
def test_forwarded_header_ignored_by_default(guarded, monkeypatch):
    """Tanpa proxy tepercaya, X-Forwarded-For (mudah dipalsukan) tidak boleh dipakai untuk menghindari limit."""
    monkeypatch.setattr(settings, "RATE_UPLOAD_PER_MIN", 2)
    codes = [post_png(guarded, headers={"X-Forwarded-For": f"9.9.9.{i}"}).status_code for i in range(3)]
    assert codes == [200, 200, 429]


def test_forwarded_header_used_behind_trusted_proxy(guarded, monkeypatch):
    monkeypatch.setattr(settings, "RATE_UPLOAD_PER_MIN", 1)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_COUNT", 1)
    # entri paling kanan = yang ditambahkan proxy kita; entri kiri bisa dipalsukan klien dan diabaikan
    a = post_png(guarded, headers={"X-Forwarded-For": "6.6.6.6, 10.0.0.1"}).status_code
    b = post_png(guarded, headers={"X-Forwarded-For": "7.7.7.7, 10.0.0.1"}).status_code
    c = post_png(guarded, headers={"X-Forwarded-For": "6.6.6.6, 10.0.0.2"}).status_code
    assert (a, b, c) == (200, 429, 200)
