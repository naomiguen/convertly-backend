import os
import pikepdf
from app.core.config import settings

def process_pdf(filename: str, action: str = "compress"):
    input_path = os.path.join(settings.UPLOAD_FOLDER, filename)
    
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"File {filename} tidak ditemukan.")

    # Tentukan nama output
    output_filename = f"processed_{filename}"
    output_path = os.path.join(settings.RESULT_FOLDER, output_filename)
    
    try:
        with pikepdf.open(input_path) as pdf:
            
            if action == "compress":
                pdf.save(
                    output_path,
                    linearize=True, 
                    compress_streams=True, 
                    object_stream_mode=pikepdf.ObjectStreamMode.generate
                )
                
            else:
                raise ValueError("Action PDF tidak dikenal.")
                
    except pikepdf.PdfError as e:
        raise ValueError(f"File PDF corrupt atau terkunci: {str(e)}")

    return {
        "status": "success",
        "original_file": filename,
        "processed_file": output_filename,
        "result_path": output_path
    }