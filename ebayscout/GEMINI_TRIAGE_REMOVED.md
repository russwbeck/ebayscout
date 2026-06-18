# Removed: direct Gemini Flash triage in `/crawl10` (preserved for restore)

This file preserves the **inline, synchronous Gemini API call** that `/crawl10`
used before the switch to the automated Drive → Gem → GCS pipeline (see
`HANDOFF`/the pipeline endpoints in `main.py`). The Gem now does the analysis
out-of-process and ebayscout consumes the result asynchronously via
`/pipeline/notify`. Everything below is the **old** path — kept verbatim so the
direct API call can be re-wired if ever wanted.

To restore: re-add `google-genai` to `requirements.txt`, re-create
`ebayscout/gemini_triage.py` from the listing below, re-add the `GEMINI_API`
secret fetch in `/internal/crawl10`, restore `_run_crawl10(gemini_api_key)` and
its inline call + the three helpers, and put `GEMINI_MODEL` back in `config.py`.

---

## `ebayscout/gemini_triage.py` (deleted module — full source)

```python
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
```

Note: `normalize_slogan` / `slogans_match` were superseded by
`ebayscout/normalize.py:normalize_key` (the single string-normalization policy
now shared by `build_slogan_year_multimap` / `resolve_with_gemini_slogans`).

---

## `_run_crawl10` call site (old) — `main.py`

`/internal/crawl10` used to fetch the secret and pass it in:

```python
try:
    gemini_api_key = _get_secret("GEMINI_API")
except Exception as exc:
    print(f"!!! CRAWL10: GEMINI_API secret unavailable — aborting: {exc}", flush=True)
    notifier.send_warning(_slack_token, _channel_id, "/crawl10: no GEMINI_API secret.")
    return jsonify({"status": "no gemini secret"}), 500
...
processed = _run_crawl10(gemini_api_key)
```

Inside the per-listing loop of `_run_crawl10(gemini_api_key)`:

```python
gemini_res = dict(gemini_triage.EMPTY_RESULT)
if result.get("first_image_bytes"):
    print(">>> CRAWL10: running Gemini triage on primary photo...", flush=True)
    gemini_res = gemini_triage.analyze_lot_with_gemini(
        result["first_image_bytes"], gemini_api_key)

_log_gemini_count(job_id, item_id, gemini_res["total_button_count"])

resolved = _gemini_resolve_yellow(job_id, item_id, result, gemini_res)
stat_gemini_resolved += len(resolved)

gemini_summary = _build_gemini_summary(gemini_res, resolved)

if result.get("yellow") or gemini_summary:
    _post_yellow_review(
        listing=listing,
        yellow_buttons=result["yellow"],
        job_id=job_id,
        confirmed_buttons=result.get("confirmed", []),
        gemini_summary=gemini_summary,
    )
```

---

## Helper functions (old) — `main.py`

```python
def _log_gemini_count(job_id, item_id, total_button_count):
    """Log Gemini's button-count estimate as its own confirm_log row
    (source='gemini_count')."""
    if match_logger is None:
        return
    check_id = f"gemini_count:{item_id}"
    try:
        rec = mlog.build_confirm_record(
            service="ebayscout", command="/crawl10", job_id=job_id, thread_ts=None,
            crop_num=None, check_id=check_id, user_id=None,
            chosen_year=None, chosen_phrase=str(total_button_count),
            chosen_type=None, source="gemini_count",
            rank_restricted=None, rank_shadow=None, shadow_leaderboard_size=None,
        )
        match_logger.log_confirmation(check_id, rec)
    except Exception as exc:
        print(f"!!! CRAWL10: gemini_count log failed for {item_id}: {exc}", flush=True)


def _gemini_resolve_yellow(job_id, item_id, result, gemini_res):
    """Promote yellow candidates whose slogan Gemini also detected; logs each
    promotion as source='gemini_verify_yes'. Mutates result in place."""
    detected = gemini_res.get("detected_slogans") or []
    if not detected:
        return []
    remaining_yellow, promoted = [], []
    for btn in result["yellow"]:
        if any(gemini_triage.slogans_match(btn["slogan"], s) for s in detected):
            promoted.append(btn)
        else:
            remaining_yellow.append(btn)
    if not promoted:
        return []
    result["yellow"] = remaining_yellow
    for btn in promoted:
        result["confirmed"].append(btn)
        if match_logger is not None:
            try:
                rec = mlog.build_confirm_record(
                    service="ebayscout", command="/crawl10", job_id=job_id, thread_ts=None,
                    crop_num=None, check_id=btn.get("check_id"), user_id=None,
                    chosen_year=btn["year"], chosen_phrase=btn["slogan"],
                    chosen_type="Football", source="gemini_verify_yes",
                    rank_restricted=None, rank_shadow=None, shadow_leaderboard_size=None,
                )
                match_logger.log_confirmation(btn.get("check_id"), rec)
            except Exception as exc:
                print(f"!!! CRAWL10: gemini_verify_yes log failed for {item_id}: {exc}", flush=True)
        enriched = _check_needed_hit(btn, buy_rules)
        if enriched is not None:
            result["needed"].append(enriched)
    return promoted


def _build_gemini_summary(gemini_res, resolved):
    """Build the '🤖 Gemini triage' summary line for the Slack yellow-review post."""
    total   = gemini_res.get("total_button_count", 0)
    blue    = gemini_res.get("blue_background_count", 0)
    white   = gemini_res.get("white_background_count", 0)
    flagged = gemini_res.get("flagged_problem_slogans") or []
    if not total and not resolved and not flagged:
        return ""
    lines = []
    if total:
        lines.append(f"🤖 *Gemini triage*: {total} button(s) ({blue} blue, {white} white)")
    if resolved:
        resolved_str = "  ·  ".join(f"{b['year']} — {b['slogan']}" for b in resolved)
        lines.append(f"✅ Auto-resolved via Gemini: {resolved_str}")
    if flagged:
        lines.append("⚠️ Gemini flagged as hard to match: " + "  ·  ".join(flagged))
    return "\n".join(lines)
```

`config.py` also defined: `GEMINI_MODEL = "gemini-2.5-flash"`.
`_post_yellow_review` is retained in `main.py` (the pipeline path reuses its
review-block builder); only its `gemini_summary` caller changed.
