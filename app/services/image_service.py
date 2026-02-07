import os
from PIL import Image
from app.core.config import settings

# Config paths
UPLOAD_DIR = "../storage/uploads"
RESULT_DIR = "../storage/results"

def process_image(filename: str, action: str = "compress"):
    
    # 1. Cek apakah file input ada
    input_path = os.path.join(settings.UPLOAD_FOLDER, filename)
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"File {filename} tidak ditemukan di uploads.")

    # 2. Buka Gambar dengan Pillow
    with Image.open(input_path) as img:
        
        # Konversi ke RGB jika gambar mode RGBA (PNG transparan)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # --- LOGIC 1: COMPRESS (Kompresi Agresif) ---
        if action == "compress":
            output_filename = f"compressed_{filename}"
            output_path = os.path.join(settings.RESULT_FOLDER, output_filename)
            
            # PERUBAHAN UTAMA:
            # 1. Resize dulu ke 70% dari ukuran asli (hemat 50% size)
            new_width = int(img.width * 0.7)
            new_height = int(img.height * 0.7)
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
            # 2. Quality turun ke 40 (dari 60) - lebih agresif
            # 3. Tambah progressive=True untuk web optimization
            img.save(output_path, "JPEG", quality=40, optimize=True, progressive=True)
            
        # --- LOGIC 2: RESIZE (Kecilkan Dimensi) ---
        elif action == "resize":
            output_filename = f"resized_{filename}"
            output_path = os.path.join(settings.RESULT_FOLDER, output_filename)
            
            # Resize max lebar/tinggi 800px
            img.thumbnail((800, 800), Image.Resampling.LANCZOS) 
            img.save(output_path, "JPEG", quality=85, optimize=True)

        # --- LOGIC 3: CONVERT TO PDF ---
        elif action == "pdf":
            base_name = os.path.splitext(filename)[0]
            output_filename = f"{base_name}.pdf"
            output_path = os.path.join(settings.RESULT_FOLDER, output_filename)
            
            # Resize dulu sebelum convert PDF
            img.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
            img.save(output_path, "PDF", resolution=72.0, optimize=True)

        else:
            raise ValueError("Action tidak dikenali")

    return {
        "status": "success",
        "original_file": filename,
        "processed_file": output_filename,
        "result_path": output_path
    }