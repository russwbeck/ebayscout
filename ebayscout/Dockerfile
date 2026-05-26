# eBay Button Scout — Cloud Run Service
# Build context: repo root (buybot/)

FROM python:3.11-slim

WORKDIR /app

# System deps (same as buybot/buttonmatcher)
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip

COPY ebayscout/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY ebayscout/ ./ebayscout/

CMD ["gunicorn", "--config", "ebayscout/gunicorn.conf.py", "ebayscout.main:flask_app"]
