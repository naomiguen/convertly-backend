import io
import os

import pymupdf
import pytest
from PIL import Image, ImageDraw

from app.core.config import settings
from app.services import photo_service
from tests.test_api import client, download, pdf_bytes, png_bytes, process, upload  # noqa: F401


def text_pdf(pages=3, rotate_page=None) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), f"Isi halaman {i + 1}", fontsize=14)
    if rotate_page is not None:
        doc[rotate_page].set_rotation(90)
    return doc.tobytes()


def pdf_texts(content: bytes) -> list[str]:
    with pymupdf.open(stream=content, filetype="pdf") as doc:
        return [page.get_text() for page in doc]


# ------------------------------------------------------------ page numbers
def test_page_numbers_default(client):
    n = upload(client, "a.pdf", text_pdf(3))
    r = process(client, "page-numbers", [n])
    assert r.json()["results"][0]["download_name"] == "doc0_numbered.pdf"
    texts = pdf_texts(download(client, r).content)
    assert [t.split()[-1] for t in texts] == ["1", "2", "3"]


def test_page_numbers_skip_cover_and_n_of_total(client):
    n = upload(client, "a.pdf", text_pdf(4))
    r = process(client, "page-numbers", [n], from_page=2, start=1, format="n_of_total", position="top-right")
    texts = pdf_texts(download(client, r).content)
    assert "/" not in texts[0]                                  # cover tanpa nomor
    assert [t.strip().splitlines()[-1] for t in texts[1:]] == ["1 / 3", "2 / 3", "3 / 3"]


def test_page_numbers_on_rotated_page(client):
    n = upload(client, "a.pdf", text_pdf(2, rotate_page=1))
    r = process(client, "page-numbers", [n])
    content = download(client, r).content
    assert "2" in pdf_texts(content)[1].split()
    with pymupdf.open(stream=content, filetype="pdf") as doc:
        assert doc[1].rotation == 0                              # dinormalkan, tampilan tetap


@pytest.mark.parametrize("opts", [{"position": "middle"}, {"format": "roman"}, {"from_page": 9}, {"start": "x"}])
def test_page_numbers_validation(client, opts):
    n = upload(client, "a.pdf", text_pdf(2))
    assert process(client, "page-numbers", [n], **opts).status_code == 400


# --------------------------------------------------------------- watermark
def test_watermark_on_every_page(client):
    n = upload(client, "a.pdf", text_pdf(3))
    r = process(client, "watermark", [n], text="DRAFT", layout="diagonal", opacity=30, color="red")
    assert r.json()["results"][0]["download_name"] == "doc0_watermarked.pdf"
    assert all("DRAFT" in t for t in pdf_texts(download(client, r).content))


def test_watermark_horizontal_keeps_original_text(client):
    n = upload(client, "a.pdf", text_pdf(1))
    r = process(client, "watermark", [n], text="RAHASIA", layout="horizontal")
    text = pdf_texts(download(client, r).content)[0]
    assert "RAHASIA" in text and "Isi halaman 1" in text


@pytest.mark.parametrize("opts", [
    {"text": ""}, {"text": "   "}, {"text": "x" * 61}, {"text": "日本語"},
    {"text": "A", "layout": "spiral"}, {"text": "A", "color": "pink"}, {"text": "A", "opacity": 0},
])
def test_watermark_validation(client, opts):
    n = upload(client, "a.pdf", text_pdf(1))
    assert process(client, "watermark", [n], **opts).status_code == 400


def test_stamping_rejects_password_pdf(client):
    n = upload(client, "a.pdf", pdf_bytes(1))
    locked = download(client, process(client, "protect", [n], password="rahasia123")).content
    n2 = upload(client, "locked.pdf", locked)
    r = process(client, "watermark", [n2], text="DRAFT")
    assert r.status_code == 400 and "password" in r.json()["detail"].lower()


# ----------------------------------------------------------------- pas foto
def portrait_png() -> bytes:
    """Foto sintetis: latar abu-abu, kepala (elips) dan badan."""
    img = Image.new("RGB", (600, 800), (200, 200, 200))
    d = ImageDraw.Draw(img)
    d.rectangle([120, 500, 480, 800], fill=(30, 40, 90))
    d.ellipse([210, 180, 390, 420], fill=(220, 170, 140))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture()
def fake_face(monkeypatch):
    monkeypatch.setattr(photo_service, "_detect_face", lambda bgr: (210.0, 180.0, 180.0, 240.0))


def result_image(client, r) -> Image.Image:
    return Image.open(io.BytesIO(download(client, r).content))


@pytest.mark.parametrize("size,px", [("2x3", (236, 354)), ("3x4", (354, 472)), ("4x6", (472, 709))])
def test_id_photo_size_dpi_and_background(client, fake_face, size, px):
    n = upload(client, "me.png", portrait_png())
    r = process(client, "id-photo", [n], size=size, background="red")
    assert r.json()["results"][0]["download_name"] == f"doc0_pasfoto_{size}.jpg"
    img = result_image(client, r)
    assert img.size == px
    assert round(img.info["dpi"][0]) == 300
    red = photo_service.BACKGROUNDS["red"]
    for corner in [(2, 2), (px[0] - 3, 2)]:
        assert all(abs(a - b) < 25 for a, b in zip(img.getpixel(corner), red))
    center = img.getpixel((px[0] // 2, int(px[1] * 0.45)))
    assert center[0] > 150 and center[1] > 100          # kulit, bukan latar merah


def test_id_photo_blue_and_keep_background(client, fake_face):
    n = upload(client, "me.png", portrait_png())
    blue = result_image(client, process(client, "id-photo", [n], background="blue")).getpixel((2, 2))
    assert blue[2] > 150 and blue[0] < 60
    n = upload(client, "me.png", portrait_png())
    kept = result_image(client, process(client, "id-photo", [n], background="keep")).getpixel((2, 2))
    assert all(abs(c - 200) < 15 for c in kept)          # latar asli tidak diubah


def test_id_photo_sheet_has_multiple_copies(client, fake_face):
    n = upload(client, "me.png", portrait_png())
    r = process(client, "id-photo", [n], size="3x4", output="sheet-4r")
    img = result_image(client, r)
    assert sorted(img.size) == [1181, 1772]
    assert r.json()["results"][0]["download_name"].endswith("_pasfoto_3x4_4r.jpg")
    assert photo_service.PAPERS_CM["sheet-a4"] == (21, 29.7)


def test_id_photo_no_face_errors_when_background_requested(client, monkeypatch):
    monkeypatch.setattr(photo_service, "_detect_face", lambda bgr: None)
    n = upload(client, "pemandangan.png", png_bytes(size=(500, 500)))
    r = process(client, "id-photo", [n], background="red")
    assert r.status_code == 400 and "Wajah tidak terdeteksi" in r.json()["detail"]


def test_id_photo_no_face_keep_background_center_crops_with_note(client, monkeypatch):
    monkeypatch.setattr(photo_service, "_detect_face", lambda bgr: None)
    n = upload(client, "x.png", png_bytes(size=(500, 500)))
    r = process(client, "id-photo", [n], background="keep")
    assert r.json()["results"][0]["note"] == "face_not_found"
    assert result_image(client, r).size == (354, 472)


@pytest.mark.parametrize("opts", [{"size": "10x10"}, {"background": "green"}, {"output": "poster"}])
def test_id_photo_validation(client, opts):
    n = upload(client, "me.png", portrait_png())
    assert process(client, "id-photo", [n], **opts).status_code == 400


def test_crop_around_face_pads_outside_image_and_keeps_aspect():
    import numpy as np
    bgr = np.zeros((400, 300, 3), np.uint8)
    crop, face = photo_service._crop_around_face(bgr, (20.0, 10.0, 100.0, 120.0), 0.75)
    assert abs(crop.shape[1] / crop.shape[0] - 0.75) < 0.01      # rasio tetap
    assert 0 <= face[0] < crop.shape[1] and 0 <= face[1] < crop.shape[0]   # wajah ada di dalam crop
