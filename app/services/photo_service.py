"""Pas foto: crop otomatis berdasarkan wajah, ganti warna latar, dan lembar cetak.

Deteksi wajah memakai YuNet (model ONNX kecil, dibundel di app/assets).
Pemisahan latar memakai GrabCut OpenCV (tanpa model tambahan). Hasil terbaik untuk foto
menghadap depan dengan latar cukup polos dan cahaya merata; rambut/tepi bisa kurang halus.
"""
import os
import cv2
import numpy as np
from PIL import Image, ImageDraw

from app.services import image_service
from app.services.output import Output, new_result

MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "face_detection_yunet_2023mar.onnx")

DPI = 300
SIZES_CM = {"2x3": (2, 3), "3x4": (3, 4), "4x6": (4, 6)}
BACKGROUNDS = {"red": (210, 20, 34), "blue": (20, 70, 190), "white": (255, 255, 255)}
PAPERS_CM = {"sheet-4r": (10, 15), "sheet-a4": (21, 29.7)}

WORK_MAX_SIDE = 1600
SEG_HEIGHT = 400             # GrabCut dijalankan di resolusi kecil (cepat), alpha lalu di-upscale
GRABCUT_ITERATIONS = 5


def _px(cm: float) -> int:
    return round(cm / 2.54 * DPI)


def _detect_face(bgr: np.ndarray):
    """Return (x, y, w, h) wajah terbaik, atau None."""
    h, w = bgr.shape[:2]
    detector = cv2.FaceDetectorYN.create(MODEL_PATH, "", (w, h), 0.7, 0.3, 5000)
    _, faces = detector.detect(bgr)
    if faces is None or len(faces) == 0:
        return None
    best = max(faces, key=lambda f: f[2] * f[3] * f[-1])
    return tuple(float(v) for v in best[:4])


def _crop_around_face(bgr, face, aspect):
    """Crop rasio `aspect` (lebar/tinggi): kepala sekitar 65% tinggi foto, dagu sekitar 74% dari atas.
    Area di luar gambar diisi dengan menyalin piksel tepi. Return (crop, face_di_crop)."""
    fx, fy, fw, fh = face
    crop_h = fh * 2.1
    crop_w = crop_h * aspect
    x0 = fx + fw / 2 - crop_w / 2
    y0 = fy - 0.55 * fh
    h, w = bgr.shape[:2]
    pad_l, pad_t = max(0, int(np.ceil(-x0))), max(0, int(np.ceil(-y0)))
    pad_r = max(0, int(np.ceil(x0 + crop_w - w)))
    pad_b = max(0, int(np.ceil(y0 + crop_h - h)))
    if pad_l or pad_t or pad_r or pad_b:
        bgr = cv2.copyMakeBorder(bgr, pad_t, pad_b, pad_l, pad_r, cv2.BORDER_REPLICATE)
        x0, y0 = x0 + pad_l, y0 + pad_t
    xi, yi = int(round(x0)), int(round(y0))
    crop = bgr[yi:yi + int(round(crop_h)), xi:xi + int(round(crop_w))]
    return crop, (fx + pad_l - xi, fy + pad_t - yi, fw, fh)


def _center_crop(bgr, aspect):
    h, w = bgr.shape[:2]
    if w / h > aspect:
        new_w = int(h * aspect)
        x0 = (w - new_w) // 2
        return bgr[:, x0:x0 + new_w]
    new_h = int(w / aspect)
    y0 = (h - new_h) // 2
    return bgr[y0:y0 + new_h, :]


def _segment_person(crop: np.ndarray, face) -> np.ndarray:
    """Alpha matte float32 0..1 (1 = orang) untuk crop pas foto."""
    h, w = crop.shape[:2]
    fx, fy, fw, fh = face
    fcx, fcy = fx + fw / 2, fy + fh / 2

    mask = np.full((h, w), cv2.GC_PR_BGD, np.uint8)
    # Area kemungkinan orang: kepala (elips) + bahu/badan ke bawah
    cv2.ellipse(mask, (int(fcx), int(fcy)), (int(fw * 0.95), int(fh * 1.1)), 0, 0, 360, cv2.GC_PR_FGD, -1)
    body_top = int(fy + fh * 1.05)
    cv2.rectangle(mask, (int(max(0, fcx - fw * 1.9)), body_top), (int(min(w, fcx + fw * 1.9)), h), cv2.GC_PR_FGD, -1)
    # Pasti orang: inti wajah dan badan tengah
    cv2.ellipse(mask, (int(fcx), int(fcy)), (int(fw * 0.45), int(fh * 0.6)), 0, 0, 360, cv2.GC_FGD, -1)
    cv2.rectangle(mask, (int(max(0, fcx - fw * 0.7)), int(fy + fh * 1.3)), (int(min(w, fcx + fw * 0.7)), h), cv2.GC_FGD, -1)
    # Pasti latar: pita tepi atas/kiri/kanan
    band_x, band_y = max(2, int(w * 0.03)), max(2, int(h * 0.02))
    mask[:band_y, :] = cv2.GC_BGD
    mask[:, :band_x] = cv2.GC_BGD
    mask[:, w - band_x:] = cv2.GC_BGD

    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    cv2.grabCut(crop, mask, None, bgd, fgd, GRABCUT_ITERATIONS, cv2.GC_INIT_WITH_MASK)
    fg = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)

    # Ambil komponen terbesar (orangnya), tutup lubang, haluskan tepi
    count, labels, stats, _ = cv2.connectedComponentsWithStats(fg, connectivity=8)
    if count <= 1:
        raise ValueError("Gagal memisahkan latar dari foto. Coba foto dengan latar lebih polos.")
    largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    fg = np.where(labels == largest, 255, 0).astype(np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, kernel)
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, kernel)
    holes = fg.copy()
    flood = np.zeros((h + 2, w + 2), np.uint8)
    cv2.floodFill(holes, flood, (0, 0), 255)
    fg = fg | cv2.bitwise_not(holes)          # lubang yang tidak terhubung ke tepi = bagian orang

    coverage = float(np.count_nonzero(fg)) / fg.size
    if coverage < 0.08 or coverage > 0.97:
        raise ValueError("Gagal memisahkan latar dari foto. Coba foto dengan latar lebih polos dan cahaya merata.")

    fg = cv2.erode(fg, np.ones((3, 3), np.uint8), iterations=1)   # buang 'halo' warna latar lama
    return cv2.GaussianBlur(fg, (0, 0), 1.2).astype(np.float32) / 255.0


def _make_sheet(photo: Image.Image, paper_key: str) -> Image.Image:
    pw, ph = photo.size
    margin, gap = _px(0.5), _px(0.2)
    best = None
    for paper_cm in (PAPERS_CM[paper_key], PAPERS_CM[paper_key][::-1]):   # portrait & landscape
        W, H = _px(paper_cm[0]), _px(paper_cm[1])
        cols = (W - 2 * margin + gap) // (pw + gap)
        rows = (H - 2 * margin + gap) // (ph + gap)
        if best is None or cols * rows > best[0] * best[1]:
            best = (cols, rows, W, H)
    cols, rows, W, H = best
    if cols < 1 or rows < 1:
        raise ValueError("Ukuran foto terlalu besar untuk kertas ini.")
    sheet = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    grid_w, grid_h = cols * pw + (cols - 1) * gap, rows * ph + (rows - 1) * gap
    ox, oy = (W - grid_w) // 2, (H - grid_h) // 2
    for r in range(rows):
        for c in range(cols):
            x, y = ox + c * (pw + gap), oy + r * (ph + gap)
            sheet.paste(photo, (x, y))
            draw.rectangle([x - 1, y - 1, x + pw, y + ph], outline=(190, 190, 190), width=1)   # garis potong
    return sheet


def id_photo(filename: str, options: dict) -> Output:
    size_key = options.get("size", "3x4")
    if size_key not in SIZES_CM:
        raise ValueError("Ukuran foto harus 2x3, 3x4, atau 4x6.")
    background = options.get("background", "red")
    if background not in BACKGROUNDS and background != "keep":
        raise ValueError("Warna latar tidak dikenal.")
    output = options.get("output", "single")
    if output != "single" and output not in PAPERS_CM:
        raise ValueError("Jenis output tidak dikenal.")

    cm_w, cm_h = SIZES_CM[size_key]
    final_w, final_h = _px(cm_w), _px(cm_h)
    aspect = cm_w / cm_h

    pil = image_service._flatten(image_service._open(filename))
    if max(pil.size) > WORK_MAX_SIDE:
        pil.thumbnail((WORK_MAX_SIDE, WORK_MAX_SIDE), Image.Resampling.LANCZOS)
    bgr = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)

    note = None
    face = _detect_face(bgr)
    if face is None:
        if background != "keep":
            raise ValueError(
                "Wajah tidak terdeteksi. Gunakan foto yang menghadap depan, wajah jelas, dan cahaya cukup."
            )
        crop, note = _center_crop(bgr, aspect), "face_not_found"
        result = cv2.resize(crop, (final_w, final_h), interpolation=cv2.INTER_AREA)
    else:
        crop, face_in_crop = _crop_around_face(bgr, face, aspect)
        crop_h = crop.shape[0]
        interp = cv2.INTER_AREA if crop_h > final_h else cv2.INTER_CUBIC
        result = cv2.resize(crop, (final_w, final_h), interpolation=interp)
        if background != "keep":
            seg_h, seg_w = SEG_HEIGHT, int(round(SEG_HEIGHT * aspect))
            small = cv2.resize(crop, (seg_w, seg_h), interpolation=cv2.INTER_AREA)
            face_small = tuple(v * seg_h / crop_h for v in face_in_crop)
            alpha_small = _segment_person(small, face_small)
            alpha = cv2.resize(alpha_small, (final_w, final_h), interpolation=cv2.INTER_LINEAR)[..., None]
            bg_bgr = np.array(BACKGROUNDS[background][::-1], np.float32).reshape(1, 1, 3)
            result = (result.astype(np.float32) * alpha + bg_bgr * (1 - alpha)).clip(0, 255).astype(np.uint8)

    photo = Image.fromarray(cv2.cvtColor(result, cv2.COLOR_BGR2RGB))
    if output == "single":
        out, image = new_result(".jpg", f"_pasfoto_{size_key}"), photo
    else:
        out, image = new_result(".jpg", f"_pasfoto_{size_key}_{output.split('-')[1]}"), _make_sheet(photo, output)
    image.save(out.path, "JPEG", quality=95, dpi=(DPI, DPI))
    out.note = note
    return out
