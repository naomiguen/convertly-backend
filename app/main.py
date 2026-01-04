import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.api.v1.endpoints import router as api_router
from app.core.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Server starting up")
    
    # Buat folder jika belum ada 
    if not os.path.exists(settings.UPLOAD_FOLDER):
        os.makedirs(settings.UPLOAD_FOLDER)
        print(f"Created: {settings.UPLOAD_FOLDER}")
        
    if not os.path.exists(settings.RESULT_FOLDER):
        os.makedirs(settings.RESULT_FOLDER)
        print(f"Created: {settings.RESULT_FOLDER}")
        
    yield # Titik ini menandakan server sedang berjalan
    print("Server shutting down")
    
app = FastAPI(
    title="Convertly API",
    description="Backend untuk file processing (PDF, Image, Docx)",
    version="1.0.0",
    lifespan=lifespan
)

#CORS (Cross-Origin resource sharing)
origins = [
    "http://localhost:5173", # Alamat default Vite React
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],       
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1", tags=["Files"])

@app.get("/")
def read_root():
    return {
        "status": "online",
        "message": "Convertly API is running smoothly!",
        "version": "1.0.0"
    }
    