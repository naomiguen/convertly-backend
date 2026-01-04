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
    # 'with' memastikan file ditutup otomatis setelah selesai (mencegah memory leak)
    with Image.open(input_path) as img:
        
        # Konversi ke RGB jika gambar mode RGBA (PNG transparan) agar bisa disimpan jadi JPEG/PDF
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # --- LOGIC 1: COMPRESS (Optimasi Kualitas) ---
        if action == "compress":
            # Output filename
            output_filename = f"compressed_{filename}"
            output_path = os.path.join(settings.RESULT_FOLDER, output_filename)
            
            # Save dengan quality=60 (turunkan kualitas 40% untuk hemat size)
            # optimize=True membuat Pillow bekerja ekstra mencari cara hemat byte
            img.save(output_path, "JPEG", quality=60, optimize=True)
            
        # --- LOGIC 2: RESIZE (Kecilkan Dimensi) ---
        elif action == "resize":
            output_filename = f"resized_{filename}"
            output_path = os.path.join(settings.RESULT_FOLDER, output_filename)
            
            # Resize max lebar/tinggi 800px, tapi tetap jaga aspek rasio
            img.thumbnail((800, 800)) 
            img.save(output_path, "JPEG", quality=85)

        # --- LOGIC 3: CONVERT TO PDF ---
        elif action == "pdf":
            # Ganti ekstensi jadi .pdf
            base_name = os.path.splitext(filename)[0]
            output_filename = f"{base_name}.pdf"
            output_path = os.path.join(settings.RESULT_FOLDER, output_filename)
            
            img.save(output_path, "PDF", resolution=100.0)

        else:
            raise ValueError("Action tidak dikenali")

    return {
        "status": "success",
        "original_file": filename,
        "processed_file": output_filename,
        "result_path": output_path
    }