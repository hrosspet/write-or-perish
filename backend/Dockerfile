# Backend Dockerfile for Write or Perish
# Supports both Flask API server and Celery worker

FROM python:3.9-slim

# Install system dependencies including ffmpeg for audio processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libpq-dev \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code into backend/ to match the project-root import layout
# (from backend.X import Y requires backend/ to exist as a package)
COPY . ./backend/

# Flask CLI discovery (needed for flask db upgrade etc.)
ENV FLASK_APP=backend.app

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash appuser && \
    chown -R appuser:appuser /app
USER appuser

# Expose port for Flask/Gunicorn
EXPOSE 5010

# Default command (can be overridden in docker-compose)
# Using gevent workers for async SSE support (long-lived streaming connections)
CMD ["gunicorn", "--bind", "0.0.0.0:5010", "--workers", "4", "-k", "gevent", "--timeout", "900", "backend.app:app"]
