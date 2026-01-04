import os
import shutil
import uuid
from fastapi import UploadFile
from app.core.config import settings


UPLOAD_DIR = "../storage/uploads"

async def save_upload_file(file: UploadFile) -> dict:
        
    # 1. Ambil ekstensi file asli (misal: .pdf)
    # file.filename = "laporan.pdf" -> splitext -> ("laporan", ".pdf")
    _, file_extension = os.path.splitext(file.filename)
    
    # 2. Buat nama baru yang acak (UUID)
    # Hasil: "a1b2c3d4-..." + ".pdf"
    unique_filename = f"{uuid.uuid4()}{file_extension}"
    
    # 3. Tentukan alamat lengkap penyimpanan
    file_path = os.path.join(settings.UPLOAD_FOLDER, unique_filename)
    
    # 4. Tulis file ke disk (Streaming)
    # Kita pakai 'wb' (write binary).
    # shutil.copyfileobj sangat efisien untuk file besar karena dia tidak load semua ke RAM.
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    return {
        "original_name": file.filename,
        "saved_name": unique_filename,
        "file_path": file_path,
        "content_type": file.content_type
    }