# eBay Button Scout — Cloud Run Job
# Build context: repo root (buybot/)
# CMD: python run.py  (runs ebayscout/run.py → ebayscout/job.py)

FROM python:3.11-slim

WORKDIR /app

# System deps (same as buybot/buttonmatcher)
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    git \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip
RUN pip install --no-cache-dir --upgrade pip

# Install Python deps
COPY ebayscout/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the ebayscout package
COPY ebayscout/ ./ebayscout/

# Entry point
CMD ["python", "ebayscout/run.py"]
