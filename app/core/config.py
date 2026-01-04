import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    PROJECT_NAME: str = os.getenv("PROJECT_NAME", "Convertly API")
    
    # Ambil path dari .env, kalau tidak ada pakai default value (parameter ke-2)
    UPLOAD_FOLDER: str = os.getenv("UPLOAD_FOLDER", "../storage/uploads")
    RESULT_FOLDER: str = os.getenv("RESULT_FOLDER", "../storage/results")
    
    # Redis (Nanti kita pakai)
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Instansiasi settings agar bisa di-import langsung
settings = Settings()