# ── Stage: runtime ────────────────────────────────────────────────────────────
FROM python:3.12-slim

# System deps: ffmpeg for video processing, fonts, libgomp for numpy/moviepy
# build-essential + cmake + python3-dev needed for packages with C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgomp1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgl1 \
    curl \
    build-essential \
    cmake \
    gcc \
    g++ \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY ui/ ./ui/

# Create runtime directories and ensure /data is writable.
# chmod here sets the image-layer permissions (used when running without a bind
# mount). The runtime chmod in the docker-compose command handles bind-mount runs.
RUN mkdir -p /data/models/voxtral /assets/backgrounds /assets/music /assets/fonts /output \
    && chmod -R 777 /data

EXPOSE 8000 8501
