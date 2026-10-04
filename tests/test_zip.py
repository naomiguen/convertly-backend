import io
import os
import zipfile

from app.core.config import settings
from tests.test_api import client, download, png_bytes, process, upload  # noqa: F401


def make_results(client, count=3):
    """Proses `count` gambar sekaligus; return daftar hasil (processed_file, download_name)."""
    names = [upload(client, f"foto{i}.png", png_bytes(color=(10 * i, 50, 90))) for i in range(count)]
    r = client.post("/api/v1/process", json={
        "action": "convert", "filenames": names,
        "original_names": [f"foto{i}.png" for i in range(count)], "options": {"format": "jpg"},
    })
    return [(x["processed_file"], x["download_name"]) for x in r.json()["results"]]


def post_zip(client, results):
    return client.post("/api/v1/zip", json={"items": [{"file": f, "name": n} for f, n in results]})


def test_zip_contains_all_results_with_nice_names(client):
    results = make_results(client, 3)
    r = post_zip(client, results)
    assert r.status_code == 200
    body = r.json()
    assert body["download_name"] == "convertly_hasil.zip" and body["processed_size"] > 0

    d = client.get(f"/api/v1/download/{body['processed_file']}", params={"name": body["download_name"]})
    assert d.status_code == 200 and "convertly_hasil.zip" in d.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(d.content))
    assert sorted(z.namelist()) == ["foto0.jpg", "foto1.jpg", "foto2.jpg"]
    assert z.testzip() is None


def test_zip_dedupes_duplicate_names(client):
    (f1, _), (f2, _) = make_results(client, 2)
    r = post_zip(client, [(f1, "scan.jpg"), (f2, "scan.jpg")])
    d = client.get(f"/api/v1/download/{r.json()['processed_file']}")
    assert sorted(zipfile.ZipFile(io.BytesIO(d.content)).namelist()) == ["scan (2).jpg", "scan.jpg"]


def test_zip_sanitizes_names_inside_archive(client):
    (f1, _), = make_results(client, 1)
    r = post_zip(client, [(f1, "../../evil.jpg")])
    d = client.get(f"/api/v1/download/{r.json()['processed_file']}")
    assert zipfile.ZipFile(io.BytesIO(d.content)).namelist() == ["evil.jpg"]


def test_zip_rejects_unsafe_and_missing_files(client):
    assert post_zip(client, [("../../secret.txt", "x.txt")]).status_code == 400
    assert post_zip(client, [("..\\secret.png", "x.png")]).status_code == 400
    r = post_zip(client, [("00000000-0000-0000-0000-000000000000.jpg", "x.jpg")])
    assert r.status_code == 404


def test_zip_cannot_read_upload_folder(client):
    """Hanya file di folder hasil yang boleh dimasukkan; file upload tidak."""
    saved = upload(client, "rahasia.png", png_bytes())
    assert os.path.exists(os.path.join(settings.UPLOAD_FOLDER, saved))
    assert post_zip(client, [(saved, "x.png")]).status_code == 404


def test_zip_validates_item_count(client):
    assert client.post("/api/v1/zip", json={"items": []}).status_code == 422
    too_many = [{"file": "00000000-0000-0000-0000-000000000000.jpg"}] * 51
    assert client.post("/api/v1/zip", json={"items": too_many}).status_code == 422


def test_zip_result_is_cleaned_up_with_ttl(client):
    from app.services.file_service import cleanup_old_files
    import time
    r = post_zip(client, make_results(client, 2))
    path = os.path.join(settings.RESULT_FOLDER, r.json()["processed_file"])
    past = time.time() - 7200
    os.utime(path, (past, past))
    cleanup_old_files(max_age_seconds=1800)
    assert not os.path.exists(path)
