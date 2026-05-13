FROM python:3.11-slim

# Avoid Python writing .pyc and enable unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv

# Create and activate dedicated virtual environment (no global pip installs)
RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /app

# Install dependencies first (cache-friendly)
COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt

# Copy project (for prod image usage; in dev we will mount volume over /app)
COPY . /app

# Default port for this service (dev uses 8001)
EXPOSE 8001

# Default command (production-like). In dev, compose will override with --reload.
# 4 workers handle concurrent API traffic; a flock-based gate inside app.main
# guarantees only one of them starts APScheduler so cron jobs don't run N times.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "4"]


