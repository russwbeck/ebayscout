# eBay Button Scout — Cloud Run Service
# Build context: repo root.
#
# Starts from the shared base image that buttonmatcher builds (its
# base/Dockerfile): Python, the system libraries, every package either service
# pins, and the CLIP ViT-B/32 weights clip.load() reads from the model cache
# (DECISIONS.md #2, #34). Only the app code is added here, so each new version
# stores a few MB in Artifact Registry instead of the whole stack.
FROM us-docker.pkg.dev/project-60d488c5-9c8e-4acc-aac/gcr.io/button-base:20261018

WORKDIR /app

COPY ebayscout/ ./ebayscout/

# Nothing is installed here: the base must already have every pin in
# requirements.txt. If one changed, build a new base in buttonmatcher and put
# its tag in FROM above; the check prints what differs.
RUN python -I ebayscout/check_base_pins.py ebayscout/requirements.txt

CMD ["gunicorn", "--config", "ebayscout/gunicorn.conf.py", "ebayscout.main:flask_app"]
