"""
ebayscout/drive_uploader.py

Push a lot's primary photo into the Gemini pipeline by uploading it to the
shared Google Drive folder the external watcher polls. The watcher sends the
file to the user's custom Gemini Gem and writes the analysis to GCS
pipeline/output/, which the watcher then POSTs back to /pipeline/notify.

The Drive API client and credentials are imported/loaded lazily so this module
imports cleanly in environments without google-api-python-client installed
(pure-Python web sessions) — matching the lazy-import style used for torch/clip.

Credentials: a service-account JSON key stored in Secret Manager as
DRIVE_SA_JSON, scoped to ``drive.file`` (enough to create files the app owns).
The target folder (config.DRIVE_FOLDER_ID) must be shared with the SA's email
(or live on a Shared Drive the SA is a member of).

Files are named ``<PIPELINE_OBJECT_PREFIX><key>.png`` (e.g.
``ebayscout__ab12cd34ef56.png``). The prefix routes the resulting pipeline
output to ebayscout (vs buttonmatcher); the ``<key>`` is the correlation token
that maps the async result back to the originating eBay listing.
"""

import io

from . import config

_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# Cached authenticated Drive service (built once per process).
_service = None


def _get_drive_service():
    """Build (and cache) an authenticated Drive v3 service from DRIVE_SA_JSON."""
    global _service
    if _service is not None:
        return _service

    import json
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    # Imported here to avoid a hard dependency on main.py's secret helper at
    # module import time.
    from .main import _get_secret  # type: ignore

    creds_info   = json.loads(_get_secret("DRIVE_SA_JSON"))
    creds        = service_account.Credentials.from_service_account_info(creds_info)
    scoped_creds = creds.with_scopes(_DRIVE_SCOPES)
    _service     = build("drive", "v3", credentials=scoped_creds, cache_discovery=False)
    return _service


def upload_lot_image(image_bytes: bytes, key: str,
                     folder_id: str | None = None) -> str:
    """Upload one primary lot photo into the watcher's Drive folder.

    ``key`` is the correlation token; the filename embeds it so the resulting
    GCS object (pipeline/output/<PIPELINE_OBJECT_PREFIX><key>.png) maps back to
    the originating listing's pending-context blob.

    Returns the created Drive file id. Raises on failure (the caller logs +
    continues to the next lot).
    """
    from googleapiclient.http import MediaIoBaseUpload

    folder_id = folder_id or config.DRIVE_FOLDER_ID
    if not folder_id:
        raise RuntimeError("DRIVE_FOLDER_ID is not configured")

    service = _get_drive_service()
    filename = f"{config.PIPELINE_OBJECT_PREFIX}{key}.png"
    media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype="image/png",
                              resumable=False)
    body = {"name": filename, "parents": [folder_id]}
    created = service.files().create(
        body=body, media_body=media, fields="id",
        supportsAllDrives=True,
    ).execute()
    return created.get("id")
