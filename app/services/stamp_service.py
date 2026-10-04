"""Menambah teks ke halaman PDF: nomor halaman dan watermark (PyMuPDF)."""
import os
import pymupdf

from app.core.security import upload_path
from app.services.image_service import _int_option
from app.services.output import Output, new_result

PAGE_NUMBER_POSITIONS = {
    "bottom-center", "bottom-right", "bottom-left",
    "top-center", "top-right", "top-left",
}
WATERMARK_COLORS = {
    "gray": (0.45, 0.45, 0.45),
    "red": (0.80, 0.10, 0.10),
    "blue": (0.10, 0.25, 0.80),
    "black": (0.0, 0.0, 0.0),
}


def _open_for_stamping(filename: str) -> pymupdf.Document:
    """Buka PDF untuk ditambah teks. Halaman yang diputar dinormalkan (tampilan tetap sama)
    supaya koordinat teks selalu 'tegak'."""
    path = upload_path(filename)
    if not os.path.exists(path):
        raise FileNotFoundError(filename)
    try:
        doc = pymupdf.open(path)
    except Exception:
        raise ValueError("File PDF rusak atau tidak bisa dibaca.")
    if doc.needs_pass:
        doc.close()
        raise ValueError("PDF dilindungi password. Buka dulu lewat alat 'Buka Kunci PDF'.")
    for page in doc:
        if page.rotation:
            page.remove_rotation()
    return doc


def _save(doc: pymupdf.Document, suffix: str) -> Output:
    out = new_result(".pdf", suffix)
    doc.save(out.path, garbage=3, deflate=True)
    return out


def add_page_numbers(filename: str, options: dict) -> Output:
    position = options.get("position", "bottom-center")
    if position not in PAGE_NUMBER_POSITIONS:
        raise ValueError("Posisi nomor halaman tidak dikenal.")
    fmt = options.get("format", "n")
    if fmt not in ("n", "n_of_total"):
        raise ValueError("Format nomor halaman tidak dikenal.")
    start = _int_option(options, "start", 1, 0, 9999)
    from_page = _int_option(options, "from_page", 1, 1, 9999)

    with _open_for_stamping(filename) as doc:
        total_pages = len(doc)
        if from_page > total_pages:
            raise ValueError(f"PDF ini hanya punya {total_pages} halaman.")
        last_number = start + (total_pages - from_page)
        vertical, horizontal = position.split("-")
        fontsize, margin = 11, 30

        for index, page in enumerate(doc):
            if index + 1 < from_page:
                continue   # halaman sebelum from_page (mis. cover) tidak diberi nomor
            number = start + (index + 1 - from_page)
            text = str(number) if fmt == "n" else f"{number} / {last_number}"
            width = pymupdf.get_text_length(text, fontname="helv", fontsize=fontsize)
            rect = page.rect
            x = {"left": margin, "center": (rect.width - width) / 2, "right": rect.width - margin - width}[horizontal]
            y = rect.height - margin if vertical == "bottom" else margin + fontsize
            page.insert_text((x, y), text, fontsize=fontsize, fontname="helv", color=(0.1, 0.1, 0.1))
        return _save(doc, "_numbered")


def add_watermark(filename: str, options: dict) -> Output:
    text = str(options.get("text", "")).strip()
    if not text:
        raise ValueError("Isi teks watermark.")
    if len(text) > 60:
        raise ValueError("Teks watermark maksimal 60 karakter.")
    try:
        text.encode("latin-1")   # font bawaan PDF hanya mendukung huruf Latin
    except UnicodeEncodeError:
        raise ValueError("Teks watermark hanya mendukung huruf Latin (A-Z, angka, tanda baca umum).")
    layout = options.get("layout", "diagonal")
    if layout not in ("diagonal", "horizontal"):
        raise ValueError("Layout watermark tidak dikenal.")
    color = WATERMARK_COLORS.get(options.get("color", "gray"))
    if color is None:
        raise ValueError("Warna watermark tidak dikenal.")
    opacity = _int_option(options, "opacity", 30, 5, 100) / 100

    with _open_for_stamping(filename) as doc:
        unit = pymupdf.get_text_length(text, fontname="helv", fontsize=100)
        for page in doc:
            w, h = page.rect.width, page.rect.height
            target = 0.8 * w if layout == "horizontal" else 0.85 * min(w, h) * 1.414
            fontsize = max(14, min(120, 100 * target / unit))
            text_width = unit * fontsize / 100
            center = pymupdf.Point(w / 2, h / 2)
            start_point = pymupdf.Point(center.x - text_width / 2, center.y + fontsize * 0.35)
            morph = (center, pymupdf.Matrix(45)) if layout == "diagonal" else None
            page.insert_text(
                start_point, text, fontsize=fontsize, fontname="helv", color=color,
                fill_opacity=opacity, morph=morph, overlay=True,
            )
        return _save(doc, "_watermarked")
