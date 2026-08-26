FROM python:3.11-slim

WORKDIR /app

# Set timezone and install system packages (ffmpeg, curl, tzdata)
ENV TZ=Asia/Singapore
ENV PYTHONUNBUFFERED=1
ENV STORAGE_DIR=/app/data

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY static/ ./static/
COPY data/ ./data/

# Persistent data directories
RUN mkdir -p /app/data/audio /app/data/episodes /app/data/cache

EXPOSE 8000

# Container Healthcheck for automated restart & monitoring
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]