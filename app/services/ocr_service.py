"""OCR (pengenalan teks) dengan RapidOCR (model ONNX sudah ikut di paketnya, tanpa Tesseract).

- ocr_pdf  : PDF scan -> PDF yang teksnya bisa dicari/disalin (teks tak terlihat di atas gambar asli).
- ocr_text : PDF scan atau gambar -> dokumen Word / TXT yang bisa diedit.

Halaman PDF yang sudah punya teks digital tidak di-OCR (lebih cepat dan lebih akurat).
Batasan: tulisan tangan tidak didukung; layout multi-kolom/tabel dibaca urut dari atas ke bawah.
"""
import importlib.util
import os
import re
import statistics
import threading
from dataclasses import dataclass

import numpy as np
import pymupdf

from app.core.config import settings
from app.core.security import upload_path
from app.services import image_service
from app.services.output import Output, new_result
from app.services.stamp_service import _open_for_stamping, _save

OCR_DPI = 250
MAX_RENDER_SIDE = 3200       # piksel; halaman sangat besar dirender lebih kecil
MIN_SCORE = 0.5              # buang hasil OCR yang tidak yakin
HAS_TEXT_CHARS = 25          # halaman dengan teks sebanyak ini dianggap sudah punya teks digital

_engine = None
_engine_lock = threading.Lock()
_run_lock = threading.Lock()   # OCR berat di CPU: jalankan satu per satu agar server tidak macet


@dataclass
class Word:
    """Satu kotak teks hasil OCR (koordinat piksel)."""
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


def is_available() -> bool:
    return importlib.util.find_spec("rapidocr_onnxruntime") is not None


def _get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR()
        return _engine


def _recognize(bgr: np.ndarray) -> list[Word]:
    engine = _get_engine()
    with _run_lock:
        result, _ = engine(bgr)
    words = []
    for box, text, score in result or []:
        text = text.strip()
        if not text or float(score) < MIN_SCORE:
            continue
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        words.append(Word(text, min(xs), min(ys), max(xs), max(ys)))
    return words


def _render_page(page: pymupdf.Page) -> tuple[np.ndarray, float]:
    """Render halaman ke array BGR. Return (gambar, skala piksel->poin PDF)."""
    longest_pt = max(page.rect.width, page.rect.height)
    dpi = min(OCR_DPI, MAX_RENDER_SIDE / longest_pt * 72)
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    rgb = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
    return np.ascontiguousarray(rgb[:, :, ::-1]), 72.0 / dpi


def _has_text(page: pymupdf.Page) -> bool:
    return len(page.get_text().strip()) >= HAS_TEXT_CHARS


def _check_page_limit(count: int) -> None:
    if count > settings.MAX_OCR_PAGES:
        raise ValueError(
            f"OCR dibatasi {settings.MAX_OCR_PAGES} halaman per file (halaman yang perlu OCR: {count}). "
            "Pisahkan PDF dengan alat 'Pisah / Edit PDF' lalu proses per bagian."
        )


def _require_engine() -> None:
    if not is_available():
        raise ValueError("Fitur OCR belum terpasang di server (paket rapidocr-onnxruntime).")


# ------------------------------------------------------------ searchable PDF
_LATIN1_FIXES = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
    "…": "...", " ": " ",
})


def _to_latin1(text: str) -> str:
    return text.translate(_LATIN1_FIXES).encode("latin-1", "replace").decode("latin-1")


def _insert_invisible(page: pymupdf.Page, word: Word, scale: float) -> None:
    """Tulis teks tak terlihat tepat di atas kotak teks hasil OCR (lebar disesuaikan dengan kotak)."""
    text = _to_latin1(word.text)
    box_w, box_h = (word.x1 - word.x0) * scale, word.h * scale
    fontsize = max(4.0, box_h * 0.8)
    natural_w = pymupdf.get_text_length(text, fontname="helv", fontsize=fontsize)
    if natural_w <= 0 or box_w <= 0:
        return
    origin = pymupdf.Point(word.x0 * scale, word.y1 * scale - box_h * 0.2)
    page.insert_text(
        origin, text, fontsize=fontsize, fontname="helv", render_mode=3,   # 3 = tidak terlihat
        morph=(origin, pymupdf.Matrix(box_w / natural_w, 1)),
    )


def ocr_pdf(filename: str, options: dict) -> Output:
    _require_engine()
    with _open_for_stamping(filename) as doc:
        todo = [page for page in doc if not _has_text(page)]
        _check_page_limit(len(todo))
        for page in todo:
            image, scale = _render_page(page)
            for word in _recognize(image):
                _insert_invisible(page, word, scale)
        out = _save(doc, "_ocr")
    if not todo:
        out.note = "already_searchable"
    return out


# ----------------------------------------------------------- scan -> teks/Word
def group_paragraphs(words: list[Word]) -> list[str]:
    """Susun kotak teks menjadi paragraf: kelompokkan per baris, lalu gabungkan baris yang
    saling menyambung (baris sebelumnya penuh sampai margin kanan dan jarak antarbaris rapat)."""
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (w.cy, w.x0))
    rows: list[list[Word]] = []
    for w in ordered:
        if rows:
            row = rows[-1]
            row_cy = sum(x.cy for x in row) / len(row)
            if abs(w.cy - row_cy) < 0.5 * max(w.h, max(x.h for x in row)):
                row.append(w)
                continue
        rows.append([w])

    lines = []
    for row in rows:
        row.sort(key=lambda w: w.x0)
        lines.append({
            "text": " ".join(w.text for w in row),
            "x0": row[0].x0, "x1": max(w.x1 for w in row),
            "y0": min(w.y0 for w in row), "y1": max(w.y1 for w in row),
            "h": statistics.median(w.h for w in row),
        })

    left = min(l["x0"] for l in lines)
    right = max(l["x1"] for l in lines)
    typical_h = statistics.median(l["h"] for l in lines)

    paragraphs: list[str] = []
    current: list[str] = []
    for i, line in enumerate(lines):
        if current:
            prev = lines[i - 1]
            big_gap = line["y0"] - prev["y1"] > 0.7 * typical_h
            prev_is_short = (prev["x1"] - left) < 0.8 * (right - left)
            if big_gap or prev_is_short:
                paragraphs.append(" ".join(current))
                current = []
        current.append(line["text"])
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _pdf_page_paragraphs(page: pymupdf.Page) -> list[str]:
    """Teks digital halaman -> daftar paragraf (satu blok = satu paragraf)."""
    blocks = page.get_text("blocks", sort=True)
    return [" ".join(b[4].split()) for b in blocks if b[6] == 0 and b[4].strip()]


def _pages_from_pdf(filename: str) -> list[list[str]]:
    path = upload_path(filename)
    if not os.path.exists(path):
        raise FileNotFoundError(filename)
    try:
        doc = pymupdf.open(path)
    except Exception:
        raise ValueError("File PDF rusak atau tidak bisa dibaca.")
    with doc:
        if doc.needs_pass:
            raise ValueError("PDF dilindungi password. Buka dulu lewat alat 'Buka Kunci PDF'.")
        _check_page_limit(sum(1 for page in doc if not _has_text(page)))
        pages = []
        for page in doc:
            if _has_text(page):
                pages.append(_pdf_page_paragraphs(page))
            else:
                image, _ = _render_page(page)
                pages.append(group_paragraphs(_recognize(image)))
    return pages


def _pages_from_image(filename: str) -> list[list[str]]:
    pil = image_service._flatten(image_service._open(filename))
    bgr = np.ascontiguousarray(np.array(pil)[:, :, ::-1])
    return [group_paragraphs(_recognize(bgr))]


def ocr_text(filename: str, options: dict) -> Output:
    fmt = str(options.get("format", "docx")).lower()
    if fmt not in ("docx", "txt"):
        raise ValueError("Format hasil harus docx atau txt.")
    _require_engine()

    if filename.lower().endswith(".pdf"):
        pages = _pages_from_pdf(filename)
    else:
        pages = _pages_from_image(filename)
    pages = [[_XML_BAD.sub("", p) for p in paras] for paras in pages]
    if not any(paras for paras in pages):
        raise ValueError("Tidak ada teks yang terdeteksi. Pastikan gambar jelas, lurus, dan tidak buram.")

    out = new_result(f".{fmt}", "_teks")
    if fmt == "txt":
        with open(out.path, "w", encoding="utf-8") as f:
            f.write("\n\n".join("\n\n".join(paras) for paras in pages if paras))
        return out

    import docx
    document = docx.Document()
    non_empty = [paras for paras in pages if paras]
    for index, paras in enumerate(non_empty):
        for text in paras:
            document.add_paragraph(text)
        if index < len(non_empty) - 1:
            document.add_page_break()
    document.save(out.path)
    return out
