import io
import os
import shutil
from PIL import Image, ImageOps

from app.core.security import upload_path
from app.services.output import Output, new_result

Image.MAX_IMAGE_PIXELS = 100_000_000  # tolak "decompression bomb"

QUALITY_PRESETS = {"low": 45, "medium": 70, "high": 85}   # low = file paling kecil
A4_PX = (1240, 1754)                                       # A4 @150 dpi
MAX_SIDE = 10000


def _open(filename: str) -> Image.Image:
    path = upload_path(filename)
    if not os.path.exists(path):
        raise FileNotFoundError(filename)
    try:
        img = Image.open(path)
        img.load()
    except Exception:
        raise ValueError("File gambar rusak atau tidak bisa dibaca.")
    # Foto HP sering disimpan "miring" lewat EXIF -> putar sesuai aslinya
    return ImageOps.exif_transpose(img)


def _has_alpha(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)


def _flatten(img: Image.Image) -> Image.Image:
    """Ubah ke RGB; area transparan jadi putih (bukan hitam)."""
    if _has_alpha(img):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return img.convert("RGB")


def _encode(img: Image.Image, fmt: str, quality: int) -> bytes:
    buf = io.BytesIO()
    if fmt == "JPEG":
        _flatten(img).save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    elif fmt == "WEBP":
        img.convert("RGBA" if _has_alpha(img) else "RGB").save(buf, "WEBP", quality=quality, method=6)
    else:  # PNG
        img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _ext_for(fmt: str) -> str:
    return {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}[fmt]


def _int_option(options: dict, key: str, default: int | None, lo: int, hi: int) -> int | None:
    value = options.get(key)
    if value in (None, ""):
        return default
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Nilai '{key}' harus berupa angka.")
    if not lo <= value <= hi:
        raise ValueError(f"Nilai '{key}' harus antara {lo} dan {hi}.")
    return value


# ---------------------------------------------------------------- compress
def compress_image(filename: str, options: dict) -> Output:
    img = _open(filename)
    src_size = os.path.getsize(upload_path(filename))
    src_ext = os.path.splitext(filename)[1].lower()

    # PNG tanpa transparansi (foto/scan) jauh lebih kecil jika jadi JPEG.
    # PNG transparan dan WEBP tetap di format aslinya.
    if src_ext == ".png":
        fmt = "PNG" if _has_alpha(img) else "JPEG"
    elif src_ext == ".webp":
        fmt = "WEBP"
    else:
        fmt = "JPEG"

    level = options.get("level", "medium")
    if level not in QUALITY_PRESETS:
        raise ValueError("Level kompresi tidak dikenal.")
    quality = QUALITY_PRESETS[level]
    target_kb = _int_option(options, "target_kb", None, 5, 100_000)

    note = None
    if fmt == "PNG":
        # PNG lossless; kompresi tambahan lewat pengurangan warna (palette 256)
        data = _encode(img, "PNG", quality)
        if len(data) >= src_size or target_kb:
            quant = img.convert("RGBA").quantize(colors=256, method=Image.Quantize.FASTOCTREE)
            data2 = _encode(quant, "PNG", quality)
            data = min(data, data2, key=len)
    elif target_kb:
        data, note = _fit_to_target(img, fmt, target_kb * 1024)
    else:
        data = _encode(img, fmt, quality)

    if len(data) >= src_size and not target_kb:
        # Hasil malah lebih besar -> kembalikan file asli apa adanya
        out = new_result(".jpg" if src_ext == ".jpeg" else src_ext, "_compressed")
        shutil.copyfile(upload_path(filename), out.path)
        out.note = "already_optimal"
        return out
    out = new_result(_ext_for(fmt), "_compressed")
    with open(out.path, "wb") as f:
        f.write(data)
    out.note = note
    return out


def _fit_to_target(img: Image.Image, fmt: str, target_bytes: int) -> tuple[bytes, str | None]:
    """Cari kualitas tertinggi yang masih <= target; jika kualitas minimum pun belum
    cukup, kecilkan dimensi bertahap."""
    work = img
    for _ in range(8):
        lo, hi, best = 10, 95, None
        while lo <= hi:
            mid = (lo + hi) // 2
            data = _encode(work, fmt, mid)
            if len(data) <= target_bytes:
                best, lo = data, mid + 1
            else:
                hi = mid - 1
        if best is not None:
            return best, None
        new_size = (max(1, int(work.width * 0.85)), max(1, int(work.height * 0.85)))
        if min(new_size) < 50:
            break
        work = work.resize(new_size, Image.Resampling.LANCZOS)
    return _encode(work, fmt, 10), "target_not_reached"


# ------------------------------------------------------------------ resize
def resize_image(filename: str, options: dict) -> Output:
    img = _open(filename)
    mode = options.get("mode", "percent")

    if mode == "percent":
        pct = _int_option(options, "percent", 50, 1, 500)
        new_size = (max(1, round(img.width * pct / 100)), max(1, round(img.height * pct / 100)))
    elif mode == "pixels":
        w = _int_option(options, "width", None, 1, MAX_SIDE)
        h = _int_option(options, "height", None, 1, MAX_SIDE)
        if w is None and h is None:
            raise ValueError("Isi lebar atau tinggi (piksel).")
        keep_ratio = str(options.get("keep_ratio", True)).lower() not in ("false", "0")
        if keep_ratio or w is None or h is None:
            # hanya salah satu diisi -> sisi lain menyesuaikan; keduanya diisi -> muat di kotak w×h
            scale_w = w / img.width if w else float("inf")
            scale_h = h / img.height if h else float("inf")
            scale = min(scale_w, scale_h)
            new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        else:
            new_size = (w, h)
    else:
        raise ValueError("Mode resize tidak dikenal.")

    if max(new_size) > MAX_SIDE:
        raise ValueError(f"Dimensi hasil terlalu besar (maks {MAX_SIDE}px).")

    resized = img.resize(new_size, Image.Resampling.LANCZOS)
    src_ext = os.path.splitext(filename)[1].lower()
    fmt = {".png": "PNG", ".webp": "WEBP"}.get(src_ext, "JPEG")
    out = new_result(_ext_for(fmt), f"_{new_size[0]}x{new_size[1]}")
    with open(out.path, "wb") as f:
        f.write(_encode(resized, fmt, 90))
    return out


# ----------------------------------------------------------------- convert
def convert_image(filename: str, options: dict) -> Output:
    target = str(options.get("format", "jpg")).lower()
    fmt = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}.get(target)
    if fmt is None:
        raise ValueError("Format tujuan harus jpg, png, atau webp.")
    img = _open(filename)
    out = new_result(_ext_for(fmt))
    with open(out.path, "wb") as f:
        f.write(_encode(img, fmt, 92))
    return out


# ------------------------------------------------------------ images -> pdf
def images_to_pdf(filenames: list[str], options: dict) -> Output:
    """Gabungkan satu atau lebih gambar (sesuai urutan) menjadi SATU PDF."""
    if not filenames:
        raise ValueError("Pilih minimal satu gambar.")
    page_size = options.get("page_size", "a4")
    if page_size not in ("a4", "original"):
        raise ValueError("Ukuran halaman harus 'a4' atau 'original'.")

    pages = []
    for name in filenames:
        img = _flatten(_open(name))
        if page_size == "a4":
            # Landscape -> halaman A4 landscape, agar gambar tidak mengecil
            canvas_size = A4_PX if img.height >= img.width else (A4_PX[1], A4_PX[0])
            margin = 40
            box = (canvas_size[0] - 2 * margin, canvas_size[1] - 2 * margin)
            fitted = ImageOps.contain(img, box, Image.Resampling.LANCZOS)
            page = Image.new("RGB", canvas_size, (255, 255, 255))
            page.paste(fitted, ((canvas_size[0] - fitted.width) // 2, (canvas_size[1] - fitted.height) // 2))
            img = page
        pages.append(img)

    out = new_result(".pdf", "")
    pages[0].save(out.path, "PDF", save_all=True, append_images=pages[1:], resolution=150.0, quality=88)
    return out
