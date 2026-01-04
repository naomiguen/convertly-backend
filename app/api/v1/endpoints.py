import os
from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from app.core.config import settings
from app.services.file_service import save_upload_file
from app.services.image_service import process_image
from app.services.pdf_service import process_pdf

router = APIRouter()

# Daftar ekstensi yang kita izinkan
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".docx"}

# --- ENDPOINT UPLOAD ---
@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):

    # 1. Validasi Ekstensi
    filename = file.filename.lower()
    isValid = False
    for ext in ALLOWED_EXTENSIONS:
        if filename.endswith(ext):
            isValid = True
            break
            
    if not isValid:
        raise HTTPException(status_code=400, detail="File type not allowed. Only PDF, Images, and DOCX.")

    # 2. Panggil Service untuk simpan file
    try:
        file_info = await save_upload_file(file)
        return {
            "status": "success",
            "message": "File uploaded successfully",
            "data": file_info
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload file: {str(e)}")


# --- ENDPOINT  PROCESS FILE ---

class ProcessRequest(BaseModel):
    filename: str
    action: str  # compress, resize, pdf, compress-pdf

@router.post("/process-file") 
async def process_file_endpoint(request: ProcessRequest):
   
    try:
       
        
        # Kelompok Action untuk Gambar
        if request.action in ["compress", "resize", "pdf"]:
            # Action "pdf" disini maksudnya Image -> PDF
            return process_image(request.filename, request.action)
            
        # Kelompok Action untuk PDF
        elif request.action in ["compress-pdf"]:
             # Kita bedakan nama actionnya biar jelas
            return process_pdf(request.filename, action="compress")
            
        else:
            raise ValueError("Action tidak dikenali / belum disupport.")

    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(e)}")
    
# endpoint download
@router.get("/download/{filename}")
async def download_file(filename: str):
    
    # Gabungkan path folder results dengan nama file
    file_path = os.path.join(settings.RESULT_FOLDER, filename)
    
    # Keamanan: Pastikan file benar-benar ada
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found or deleted")
    
    # FileResponse otomatis mengatur Header agar browser mendownloadnya
    return FileResponse(
        path=file_path, 
        filename=filename, # Nama file saat didownload user
        media_type='application/octet-stream' # Tipe file umum
    )