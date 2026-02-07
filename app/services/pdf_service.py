import os
import pikepdf
from app.core.config import settings

def process_pdf(filename: str, action: str = "compress"):
   
    input_path = os.path.join(settings.UPLOAD_FOLDER, filename)
    
    # Validasi file exists
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"File {filename} tidak ditemukan.")

    # Tentukan nama output
    output_filename = f"compressed_{filename}"
    output_path = os.path.join(settings.RESULT_FOLDER, output_filename)
    
    try:
        print(f"Opening PDF: {filename}")
        
        with pikepdf.open(input_path) as pdf:
            
            if action == "compress":
                print(f"🗜️  Compressing PDF...")
                
                # Kompresi PDF dengan parameter optimal
                # PENTING: linearize dan normalize_content TIDAK BISA bersamaan
                pdf.save(
                    output_path,
                    linearize=True,                     # Optimasi untuk web viewing
                    compress_streams=True,              # Kompresi semua stream
                    stream_decode_level=pikepdf.StreamDecodeLevel.generalized,  # Decode level maksimal
                    object_stream_mode=pikepdf.ObjectStreamMode.generate,       # Generate object streams
                    recompress_flate=True               # Rekompresi flate streams
                )
                
                print(f"PDF compressed successfully")
                
            else:
                raise ValueError(f"Action '{action}' tidak dikenal untuk PDF.")
                
    except pikepdf.PdfError as e:
        print(f"PDF Error: {e}")
        raise ValueError(f"File PDF corrupt atau terkunci: {str(e)}")
    
    except Exception as e:
        print(f" Unexpected Error: {e}")
        import traceback
        traceback.print_exc()
        raise ValueError(f"Error processing PDF: {str(e)}")

    return {
        "status": "success",
        "original_file": filename,
        "processed_file": output_filename,
        "result_path": output_path
    }