"""
ebayscout/gemini_triage.py

Optional Gemini 2.5 Flash triage step for /crawl10 (see main.py:_run_crawl10).
Given a lot's primary photo, asks Gemini to count buttons, estimate the
blue/white background split, and OCR the central slogan on each button.

`google.genai` is imported lazily inside analyze_lot_with_gemini() so this
module — and its pure helper functions — can be imported and unit-tested
without the dependency installed (matches the lazy-import style used for
torch/clip/cv2 in main.py).

Fail-open: any error (network, bad JSON, missing dependency) returns the same
all-zero/empty shape as a "nothing detected" result, so callers never need a
try/except around analyze_lot_with_gemini().
"""

import io
import json
import re

EMPTY_RESULT = {
    "total_button_count": 0,
    "blue_background_count": 0,
    "white_background_count": 0,
    "detected_slogans": [],
    "flagged_problem_slogans": [],
}

_TRIAGE_PROMPT = """
You are an expert inventory assistant specializing in vintage sports pinback buttons.
Analyze the provided image and extract total counts, color distribution, and primary slogans.

### Domain Context:
- The vast majority of these buttons are blue with white text.
- A small minority of buttons are white with blue text. Pay close attention to these white variants.

### Alignment & Operational Guidelines:
1. SPATIAL COUNTING STRATEGY: Scan the image methodically from top-to-bottom, left-to-right. Group the buttons mentally by rows to ensure no item is missed or double-counted.
2. COLOR BREAKDOWN: Count how many buttons have a blue background versus a white background.
3. OCR TEXT EXTRACTION: Extract the primary slogan or cheer located in the center of each button (e.g., "Stop Stanford", "Whip the Wolfpack").
4. TEXT FILTERING: Ignore any tiny, repetitive promotional or manufacturer text wrapping around the top or bottom borders (e.g., ignore "CENTRAL COUNTIES BANK SAYS" and logos). Only extract the main central slogan.
5. PROBLEM IDENTIFICATION: Identify any buttons where the central text is cut off, heavily smudged, or uses non-standard phrasing that an automated exact-match database lookup might fail to find.

### Output Requirements:
Return your response strictly as a JSON object matching this schema. Do not include markdown formatting or blocks outside the JSON object:
{
  "total_button_count": 11,
  "blue_background_count": 10,
  "white_background_count": 1,
  "detected_slogans": [
    "Stop Stanford",
    "Whip the Wolfpack"
  ],
  "flagged_problem_slogans": []
}
"""

_PUNCT_RE = re.compile(r"[^a-z0-9 ]")
_WS_RE = re.compile(r"\s+")


def normalize_slogan(s: str) -> str:
    """Lowercase, strip punctuation, and collapse whitespace for comparison."""
    s = (s or "").lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def slogans_match(a: str, b: str) -> bool:
    """True if two slogans are the same after normalization."""
    return normalize_slogan(a) == normalize_slogan(b)


def analyze_lot_with_gemini(image_bytes: bytes, api_key: str, model: str = None) -> dict:
    """Invoke Gemini to triage a lot photo: button count, color split, slogans.

    Returns a dict shaped like EMPTY_RESULT. Fails open (returns EMPTY_RESULT)
    on any error so the caller never needs to handle exceptions.
    """
    if model is None:
        from . import config
        model = config.GEMINI_MODEL

    try:
        from google import genai
        from google.genai import types
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes))
        client = genai.Client(api_key=api_key)

        response = client.models.generate_content(
            model=model,
            contents=[image, _TRIAGE_PROMPT],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )

        data = json.loads(response.text)
        return {
            "total_button_count": int(data.get("total_button_count", 0) or 0),
            "blue_background_count": int(data.get("blue_background_count", 0) or 0),
            "white_background_count": int(data.get("white_background_count", 0) or 0),
            "detected_slogans": list(data.get("detected_slogans", []) or []),
            "flagged_problem_slogans": list(data.get("flagged_problem_slogans", []) or []),
        }
    except Exception as exc:
        print(f"!!! GEMINI: triage failed: {exc}", flush=True)
        return dict(EMPTY_RESULT)
