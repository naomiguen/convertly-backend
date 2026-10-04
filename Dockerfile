FROM python:3.13-slim

WORKDIR /app

# LibreOffice untuk Word/PowerPoint -> PDF (ukuran image bertambah +/- 600 MB).
# libgl1 + libglib2.0-0 dibutuhkan OpenCV (ikut terpasang oleh paket OCR).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-writer libreoffice-impress fonts-liberation fonts-dejavu-core \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Folder penyimpanan sementara (dipasang sebagai volume oleh docker-compose)
ENV UPLOAD_FOLDER=/data/uploads \
    RESULT_FOLDER=/data/results
RUN mkdir -p /data/uploads /data/results

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
