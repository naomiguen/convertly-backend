import os
from typing import List
from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
from app.core.config import settings
from app.services.file_service import save_upload_file
from app.services.image_service import process_image
from app.services.pdf_service import process_pdf

router = APIRouter()

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".docx"}

def remove_file(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        pass


@router.post("/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    uploaded_files_info = []

    for file in files:
        filename = file.filename.lower()
        isValid = False
        for ext in ALLOWED_EXTENSIONS:
            if filename.endswith(ext):
                isValid = True
                break
        
        if not isValid:
            continue

        try:
            file_info = await save_upload_file(file)
            uploaded_files_info.append(file_info)
        except Exception:
            continue

    if not uploaded_files_info:
        raise HTTPException(
            status_code=400, 
            detail="Tidak ada file valid yang diupload."
        )
    
    return {
        "status": "success",
        "message": f"{len(uploaded_files_info)} file(s) uploaded successfully",
        "data": uploaded_files_info
    }


class ProcessRequest(BaseModel):
    filename: str
    action: str

@router.post("/process-file") 
async def process_file_endpoint(request: ProcessRequest, background_tasks: BackgroundTasks):
    upload_path = os.path.join(settings.UPLOAD_FOLDER, request.filename)
    
    try:
        response_data = {}

        if request.action in ["compress", "resize", "pdf"]:
            response_data = process_image(request.filename, request.action)
            
        elif request.action in ["compress-pdf"]:
            response_data = process_pdf(request.filename, action="compress")
            
        else:
            raise ValueError(f"Action tidak dikenali: {request.action}")

        if "processed_file" in response_data:
            output_filename = response_data["processed_file"]
            output_path = os.path.join(settings.RESULT_FOLDER, output_filename)
            
            if os.path.exists(output_path):
                file_size = os.path.getsize(output_path)
                response_data["processed_size"] = file_size
            else:
                response_data["processed_size"] = 0

        background_tasks.add_task(remove_file, upload_path)

        return response_data

    except FileNotFoundError as e:
        background_tasks.add_task(remove_file, upload_path)
        raise HTTPException(status_code=404, detail="File not found")
        
    except ValueError as e:
        background_tasks.add_task(remove_file, upload_path)
        raise HTTPException(status_code=400, detail=str(e))
        
    except Exception as e:
        background_tasks.add_task(remove_file, upload_path)
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(e)}")

    
@router.get("/download/{filename}")
async def download_file(filename: str, background_tasks: BackgroundTasks):
    file_path = os.path.join(settings.RESULT_FOLDER, filename)
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found or deleted")
    
    background_tasks.add_task(remove_file, file_path)
    
    return FileResponse(
        path=file_path, 
        filename=filename,
        media_type='application/octet-stream'
    )