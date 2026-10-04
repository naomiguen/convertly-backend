import io
import os
import shutil
import zipfile
import pikepdf
from PIL import Image

from app.core.security import upload_path
from app.services.output import Output, new_result
from app.services import image_service

# level -> (kualitas JPEG, sisi terpanjang maksimum dalam piksel)
COMPRESS_LEVELS = {
    "low": (40, 1000),      # kompresi paling kuat, file paling kecil
    "medium": (60, 1500),
    "high": (80, 2200),     # kompresi ringan, kualitas terjaga
}
_LEVEL_ORDER = ["high", "medium", "low"]
MIN_IMAGE_BYTES = 8 * 1024   # gambar kecil tidak dikompres ulang
MAX_PDF_TO_IMAGE_PAGES = 100


def _open_pdf(filename: str, password: str = "") -> pikepdf.Pdf:
    path = upload_path(filename)
    if not os.path.exists(path):
        raise FileNotFoundError(filename)
    try:
        return pikepdf.open(path, password=password)
    except pikepdf.PasswordError:
        raise ValueError("PDF dilindungi password. Buka dulu lewat alat 'Buka Kunci PDF'.")
    except pikepdf.PdfError:
        raise ValueError("File PDF rusak atau tidak bisa dibaca.")


# ---------------------------------------------------------------- compress
def _recompress_images(pdf: pikepdf.Pdf, quality: int, max_side: int) -> None:
    # SMask / ImageMask adalah mask transparansi: jangan diubah jadi JPEG
    skip = set()
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == "/Image":
            smask = obj.get("/SMask")
            if smask is not None:
                skip.add(smask.objgen)
            mask = obj.get("/Mask")
            if isinstance(mask, pikepdf.Stream):
                skip.add(mask.objgen)

    for obj in pdf.objects:
        if not (isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == "/Image"):
            continue
        if obj.objgen in skip or obj.get("/ImageMask", False):
            continue
        try:
            raw_len = len(obj.read_raw_bytes())
            if raw_len < MIN_IMAGE_BYTES:
                continue
            pil = pikepdf.PdfImage(obj).as_pil_image()
            if pil.mode not in ("RGB", "L"):
                pil = pil.convert("RGB")
            if max(pil.size) > max_side:
                scale = max_side / max(pil.size)
                pil = pil.resize(
                    (max(1, round(pil.width * scale)), max(1, round(pil.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            buf = io.BytesIO()
            pil.save(buf, "JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) >= raw_len:
                continue
            obj.write(data, filter=pikepdf.Name.DCTDecode)
            obj.Width, obj.Height = pil.size
            obj.ColorSpace = pikepdf.Name.DeviceRGB if pil.mode == "RGB" else pikepdf.Name.DeviceGray
            obj.BitsPerComponent = 8
            for key in ("/DecodeParms", "/Decode"):
                if key in obj:
                    del obj[key]
        except Exception:
            continue   # gambar yang formatnya tidak umum dilewati, bukan menggagalkan seluruh PDF


def _compress_once(filename: str, level: str, out_path: str) -> int:
    quality, max_side = COMPRESS_LEVELS[level]
    with _open_pdf(filename) as pdf:
        _recompress_images(pdf, quality, max_side)
        pdf.remove_unreferenced_resources()
        pdf.save(
            out_path,
            compress_streams=True,
            recompress_flate=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
        )
    return os.path.getsize(out_path)


def compress_pdf(filename: str, options: dict) -> Output:
    level = options.get("level", "medium")
    if level not in COMPRESS_LEVELS:
        raise ValueError("Level kompresi tidak dikenal.")
    target_kb = image_service._int_option(options, "target_kb", None, 20, 100_000)

    src = upload_path(filename)
    src_size = os.path.getsize(src)
    out = new_result(".pdf", "_compressed")

    # Tanpa target: pakai level pilihan. Dengan target: mulai dari level pilihan,
    # makin agresif sampai ukuran memenuhi.
    levels = [level] if not target_kb else _LEVEL_ORDER[_LEVEL_ORDER.index(level):]
    size = None
    for lv in levels:
        size = _compress_once(filename, lv, out.path)
        if not target_kb or size <= target_kb * 1024:
            break

    if target_kb and size > target_kb * 1024:
        out.note = "target_not_reached"
    elif size >= src_size:
        shutil.copyfile(src, out.path)   # jangan beri hasil yang lebih besar dari aslinya
        out.note = "already_optimal"
    return out


# ------------------------------------------------------------- page ranges
def parse_page_ranges(spec: str, total: int) -> list[int]:
    """'1-3,5,8-' -> [0,1,2,4,7,...] (index 0-based, urutan sesuai input, tanpa duplikat)."""
    spec = (spec or "").replace(" ", "")
    if not spec:
        raise ValueError("Isi nomor halaman, contoh: 1-3,5,8-10")
    pages: list[int] = []
    for part in spec.split(","):
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                start = int(a) if a else 1
                end = int(b) if b else total
            else:
                start = end = int(part)
        except ValueError:
            raise ValueError(f"Format halaman tidak valid: '{part}'. Contoh: 1-3,5,8-10")
        if start < 1 or end > total or start > end:
            raise ValueError(f"Halaman '{part}' di luar rentang (PDF ini punya {total} halaman).")
        for p in range(start - 1, end):
            if p not in pages:
                pages.append(p)
    if not pages:
        raise ValueError("Isi nomor halaman, contoh: 1-3,5,8-10")
    return pages


# ------------------------------------------------------------------- merge
def merge_pdfs(filenames: list[str], options: dict) -> Output:
    if len(filenames) < 2:
        raise ValueError("Pilih minimal 2 file untuk digabung.")
    merged = pikepdf.Pdf.new()
    sources = []
    temp_paths = []
    try:
        for name in filenames:
            if name.lower().endswith(".pdf"):
                src = _open_pdf(name)
            else:  # gambar ikut digabung sebagai satu halaman
                tmp = image_service.images_to_pdf([name], {"page_size": "a4"})
                temp_paths.append(tmp.path)
                src = pikepdf.open(tmp.path)
            sources.append(src)
            merged.pages.extend(src.pages)
        out = new_result(".pdf", "_merged")
        merged.save(out.path, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    finally:
        for src in sources:
            src.close()
        merged.close()
        for p in temp_paths:
            if os.path.exists(p):
                os.remove(p)
    return out


# ------------------------------------------------------------------- split
def split_pdf(filename: str, options: dict) -> Output:
    mode = options.get("mode", "extract")
    with _open_pdf(filename) as src:
        total = len(src.pages)

        if mode == "each":
            out = new_result(".zip", "_pages")
            with zipfile.ZipFile(out.path, "w", zipfile.ZIP_DEFLATED) as zf:
                width = len(str(total))
                for i in range(total):
                    single = pikepdf.Pdf.new()
                    single.pages.append(src.pages[i])
                    buf = io.BytesIO()
                    single.save(buf)
                    zf.writestr(f"halaman_{str(i + 1).zfill(width)}.pdf", buf.getvalue())
            return out

        pages = parse_page_ranges(options.get("ranges", ""), total)
        if mode == "remove":
            keep = [i for i in range(total) if i not in set(pages)]
            if not keep:
                raise ValueError("Tidak boleh menghapus semua halaman.")
            pages, suffix = keep, "_edited"
        elif mode == "extract":
            suffix = "_extract"
        else:
            raise ValueError("Mode split tidak dikenal.")

        result = pikepdf.Pdf.new()
        for i in pages:
            result.pages.append(src.pages[i])
        out = new_result(".pdf", suffix)
        result.save(out.path, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
        return out


# ------------------------------------------------------------------ rotate
def rotate_pdf(filename: str, options: dict) -> Output:
    try:
        angle = int(options.get("angle", 90))
    except (TypeError, ValueError):
        raise ValueError("Sudut rotasi tidak valid.")
    if angle not in (90, 180, 270):
        raise ValueError("Sudut rotasi harus 90, 180, atau 270.")
    with _open_pdf(filename) as pdf:
        total = len(pdf.pages)
        spec = str(options.get("pages", "")).strip()
        targets = parse_page_ranges(spec, total) if spec else range(total)
        for i in targets:
            pdf.pages[i].rotate(angle, relative=True)
        out = new_result(".pdf", "_rotated")
        pdf.save(out.path)
    return out


# ------------------------------------------------------ protect / unlock
def protect_pdf(filename: str, options: dict) -> Output:
    password = str(options.get("password", ""))
    if len(password) < 4:
        raise ValueError("Password minimal 4 karakter.")
    with _open_pdf(filename) as pdf:
        out = new_result(".pdf", "_protected")
        pdf.save(out.path, encryption=pikepdf.Encryption(user=password, owner=password, R=6))
    return out


def unlock_pdf(filename: str, options: dict) -> Output:
    password = str(options.get("password", ""))
    path = upload_path(filename)
    if not os.path.exists(path):
        raise FileNotFoundError(filename)
    try:
        pdf = pikepdf.open(path, password=password)
    except pikepdf.PasswordError:
        raise ValueError("Password salah.")
    except pikepdf.PdfError:
        raise ValueError("File PDF rusak atau tidak bisa dibaca.")
    with pdf:
        out = new_result(".pdf", "_unlocked")
        pdf.save(out.path)   # disimpan tanpa enkripsi
    return out


# ------------------------------------------------------------ pdf -> image
def pdf_to_images(filename: str, options: dict) -> Output:
    import pymupdf  # PyMuPDF

    fmt = str(options.get("format", "jpg")).lower()
    if fmt not in ("jpg", "png"):
        raise ValueError("Format harus jpg atau png.")
    dpi = image_service._int_option(options, "dpi", 150, 72, 300)

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
        if doc.page_count > MAX_PDF_TO_IMAGE_PAGES:
            raise ValueError(f"Maksimal {MAX_PDF_TO_IMAGE_PAGES} halaman per proses (PDF ini {doc.page_count}).")

        def render(page) -> bytes:
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            return pix.tobytes("jpeg", jpg_quality=88) if fmt == "jpg" else pix.tobytes("png")

        if doc.page_count == 1:
            out = new_result(f".{fmt}", "")
            with open(out.path, "wb") as f:
                f.write(render(doc[0]))
            return out

        out = new_result(".zip", "_images")
        width = len(str(doc.page_count))
        with zipfile.ZipFile(out.path, "w", zipfile.ZIP_STORED) as zf:
            for i, page in enumerate(doc):
                zf.writestr(f"halaman_{str(i + 1).zfill(width)}.{fmt}", render(page))
    return out
