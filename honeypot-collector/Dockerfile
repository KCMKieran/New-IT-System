# Honeypot Collector — small, clean, non-root image.
FROM python:3.11-slim

# Keep Python lean and predictable inside the container.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so this layer caches across code-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the single application module. Nothing else belongs in this image —
# no secrets, no DB drivers, no internal config are baked in (all via env).
COPY collector.py .

# Run as a non-root user: if the process is ever compromised, it owns nothing.
RUN useradd --create-home --uid 10001 honeypot
USER honeypot

EXPOSE 8010

CMD ["uvicorn", "collector:app", "--host", "0.0.0.0", "--port", "8010"]
