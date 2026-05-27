"""
ebayscout/clip_matcher.py

CLIP-based button matching.

Ported from buttonmatcher/main.py (match_all_crops / score_slogans) and
buybot/main.py (match_button).  Uses the same GCS files:
    vectors.pt          — image reference embeddings
    text_features.pt    — text embeddings + metadata

Scoring constants match buttonmatcher: ALPHA=0.6, BETA=0.4.

Call init(bucket_name) once per job run before calling match_crop().
"""

import os
import tempfile
from typing import Any

import numpy as np
import torch
import clip
from PIL import Image
from google.cloud import storage

from . import config

# ---------------------------------------------------------------------------
# Module-level state (populated by init())
# ---------------------------------------------------------------------------
_model        = None   # CLIP ViT-B/32 (quantized)
_preprocess   = None   # torchvision transform
_device       = "cpu"

_ref_vectors: torch.Tensor | None    = None   # [N, D] image reference vecs
_ref_labels:  list[str]              = []     # "YEAR SLOGAN" parallel to ref_vectors

_text_features: torch.Tensor | None  = None   # [M, D] text embeddings
_text_phrases:  list[str]            = []     # slogans
_text_years:    list[int]            = []     # years (int)
_text_types:    list[str]            = []     # sport types

_initialized = False


def init(bucket_name: str = config.BUCKET_NAME) -> None:
    """
    Load CLIP model and GCS vector files into module-level state.
    Must be called once before match_crop().
    """
    global _model, _preprocess, _device
    global _ref_vectors, _ref_labels
    global _text_features, _text_phrases, _text_years, _text_types
    global _initialized

    if _initialized:
        return

    print(">>> CLIP: Loading ViT-B/32 model...", flush=True)
    _model, _preprocess = clip.load("ViT-B/32", device=_device)

    # Quantize to int8 for faster CPU inference (same as both existing services)
    _model = torch.quantization.quantize_dynamic(
        _model, {torch.nn.Linear}, dtype=torch.qint8
    )
    print(">>> CLIP: Model loaded and quantized.", flush=True)

    # Download vector files from GCS
    client = storage.Client()
    bucket = client.bucket(bucket_name)

    with tempfile.TemporaryDirectory() as tmpdir:
        # --- text_features.pt ---
        text_path = os.path.join(tmpdir, "text_features.pt")
        print(">>> CLIP: Downloading text_features.pt...", flush=True)
        bucket.blob("text_features.pt").download_to_filename(text_path)
        cached = torch.load(text_path, weights_only=False, map_location="cpu")
        _text_features = cached["features"]
        _text_phrases  = list(cached["phrases"])
        _text_years    = [int(y) for y in cached["years"]]
        _text_types    = list(cached.get("types", ["Football"] * len(_text_years)))
        print(f">>> CLIP: Text embeddings loaded — {_text_features.shape[0]} entries.", flush=True)

        # --- vectors.pt ---
        vec_path = os.path.join(tmpdir, "vectors.pt")
        print(">>> CLIP: Downloading vectors.pt...", flush=True)
        bucket.blob("vectors.pt").download_to_filename(vec_path)
        cached_vecs = torch.load(vec_path, weights_only=False, map_location="cpu")
        _ref_vectors = cached_vecs["vectors"]
        _ref_labels  = list(cached_vecs["labels"])
        print(f">>> CLIP: Image reference vectors loaded — {len(_ref_labels)} entries.", flush=True)

    _initialized = True
    print(">>> CLIP: Initialization complete.", flush=True)


def match_crop(
    pil_image: Image.Image,
    threshold: float | None = None,
) -> dict | None:
    """
    Match a single PIL.Image (RGB) crop against the button database.

    Returns a dict on a match above *threshold*:
        {year: str, slogan: str, overall: float,
         image_score: float, slogan_score: float}

    Returns None if best overall score < threshold.

    threshold defaults to config.CONFIDENCE_THRESHOLD (0.72).  Pass
    config.REJECTION_THRESHOLD (0.45) to also surface low-confidence
    matches for scan-summary categorisation without triggering alerts.

    Year candidate selection uses a combined image+slogan score so that strong
    text evidence can promote the correct year even when image similarity alone
    is ambiguous between nearby years.
    """
    if threshold is None:
        threshold = config.CONFIDENCE_THRESHOLD
    if not _initialized:
        raise RuntimeError("clip_matcher.init() must be called before match_crop().")

    # Encode the crop
    tensor = _preprocess(pil_image).unsqueeze(0).to(_device)
    with torch.inference_mode():
        vec = _model.encode_image(tensor).float()
    vec = vec / vec.norm(dim=-1, keepdim=True)  # [1, D]

    # Image similarities against reference vectors
    image_sims = (vec @ _ref_vectors.T).cpu().numpy()[0]   # [N]
    # Text similarities against all text embeddings
    text_sims  = (vec @ _text_features.T).cpu().numpy()[0] # [M]

    # Build year → best image score map from reference vectors
    year_image_scores: dict[int, float] = {}
    for i, label in enumerate(_ref_labels):
        try:
            year = int(label.split()[0])
        except (ValueError, IndexError):
            continue
        score = float(image_sims[i])
        if year not in year_image_scores or score > year_image_scores[year]:
            year_image_scores[year] = score

    # Build year → best raw text similarity (before normalisation)
    # This lets slogan evidence vote on which year is the right candidate.
    years_arr = np.array(_text_years, dtype=np.int32)
    year_text_best: dict[int, float] = {}
    for year in year_image_scores:
        mask = years_arr == year
        year_text_best[year] = float(text_sims[mask].max()) if mask.any() else 0.0

    # Rank candidate years by combined image+slogan score.
    # Using the same ALPHA/BETA weights as the final formula so the year that
    # would win the overall comparison is also the one that gets promoted here.
    year_combined: dict[int, float] = {
        year: (config.ALPHA * img_score)
              + (config.BETA * _normalize_slogan(year_text_best.get(year, 0.0)))
        for year, img_score in year_image_scores.items()
    }

    # Take top 5 years by combined score (not image score alone)
    top_years = sorted(year_combined.items(), key=lambda x: x[1], reverse=True)[:5]
    # Pass the original image scores to _score_slogans — it does its own combination
    top_year_image_scores = {year: year_image_scores[year] for year, _ in top_years}
    allowed_years = set(top_year_image_scores.keys())

    # Score slogans for each candidate year
    results = _score_slogans(
        text_sims=np.array(text_sims, dtype=np.float32),
        year_scores=top_year_image_scores,
        allowed_years=allowed_years,
    )

    if not results:
        return None

    best = results[0]
    if best["overall"] < threshold:
        return None

    return {
        "year":         str(best["year"]),
        "slogan":       best["slogan"],
        "overall":      float(best["overall"]),
        "image_score":  float(best["image_score"]),
        "slogan_score": float(best["slogan_score"]),
    }


# ---------------------------------------------------------------------------
# Internal helpers (ported from buttonmatcher/main.py score_slogans)
# ---------------------------------------------------------------------------

def _score_slogans(
    text_sims: np.ndarray,
    year_scores: dict[int, float],
    allowed_years: set[int],
) -> list[dict]:
    """
    For each candidate year, find the best matching slogan and compute overall score.
    Returns results sorted by (overall, slogan_score) descending, top 3.
    """
    years_arr = np.array(_text_years, dtype=np.int32)
    allowed_arr = np.array(list(allowed_years), dtype=np.int32)
    year_mask = np.isin(years_arr, allowed_arr)

    valid_indices = np.where(year_mask)[0]
    if len(valid_indices) == 0:
        return []

    valid_years = years_arr[valid_indices]
    valid_sims  = text_sims[valid_indices]

    results = []
    for year, image_score in year_scores.items():
        if year not in allowed_years:
            continue
        match_mask = valid_years == year
        if not match_mask.any():
            best_raw    = 0.0
            best_phrase = "Unknown"
        else:
            local_sims     = valid_sims[match_mask]
            best_local_idx = int(np.argmax(local_sims))
            best_raw       = float(local_sims[best_local_idx])
            global_idx     = valid_indices[match_mask][best_local_idx]
            best_phrase    = _text_phrases[global_idx]

        slogan_score = _normalize_slogan(best_raw)
        overall      = (config.ALPHA * image_score) + (config.BETA * slogan_score)

        # Boost for near-certain text match (>90%) — ramps fast
        if slogan_score > 0.9:
            overall += (slogan_score - 0.9) * 2.5
        # Moderate boost for strong-but-not-certain text match (75–90%)
        elif slogan_score > 0.75:
            overall += (slogan_score - 0.75) * 1.2
        # Penalty for very weak text match
        if slogan_score < config.SLOGAN_PENALTY_THRESHOLD:
            overall *= config.PENALTY_MULTIPLIER

        results.append({
            "year":         year,
            "slogan":       best_phrase,
            "overall":      min(1.0, overall),
            "image_score":  image_score,
            "slogan_score": slogan_score,
        })

    results.sort(key=lambda x: (x["overall"], x["slogan_score"]), reverse=True)
    return results[:3]


def _normalize_slogan(score: float, min_s: float = 0.15, max_s: float = 0.35) -> float:
    """Normalise raw CLIP cosine similarity into [0, 1]. Identical to both services."""
    norm = (score - min_s) / (max_s - min_s)
    return max(0.0, min(1.0, norm))
