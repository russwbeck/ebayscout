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

# Pre-download CLIP ViT-B/32 weights (~338 MB) into the image so cold
# starts never hit OpenAI's CDN at runtime.  This layer is cached by
# Docker and only re-runs when requirements.txt changes.
RUN python -c "import clip; clip.load('ViT-B/32', device='cpu'); print('CLIP ViT-B/32 cached.')"

COPY ebayscout/ ./ebayscout/

CMD ["gunicorn", "--config", "ebayscout/gunicorn.conf.py", "ebayscout.main:flask_app"]
