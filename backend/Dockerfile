# Backend: FastAPI + Whisper + Ollama client
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps: ffmpeg (Whisper), git (some pip wheels), curl (healthcheck), tini (PID1)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app

RUN mkdir -p /app/logs /root/.cache/whisper

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command (overridden by docker-compose to also tee logs to file)
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
