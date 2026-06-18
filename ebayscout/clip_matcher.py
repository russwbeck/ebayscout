"""
ebayscout/clip_matcher.py

CLIP-based button matching.

Ported from buttonmatcher/main.py (match_all_crops / score_slogans) and
buybot/main.py (match_button).  Uses the same GCS files:
    vectors.pt          — image reference embeddings
    text_features.pt    — text embeddings + metadata

Scoring mirrors buttonmatcher EXACTLY: ALPHA=BETA=0.5, a single >0.9 text boost,
a <0.3 text penalty, a rarity tiebreaker, and dual-signal year selection — so the
GREEN/AUTO confidence tiers (config.GREEN_THRESHOLD / AUTO_RESOLVE_THRESHOLD)
transfer directly and match_logging.build_leaderboard scores equal the live ones.

Call init(bucket_name) once per job run before calling match_crop().
"""

import os
import tempfile
import threading
from typing import Any

import numpy as np
import torch
import clip
from PIL import Image
from google.cloud import storage

from . import config
from . import match_logging
from . import scoring
from . import rerank
from . import normalize
from .scoring import tokenize, rarity_weight, STOPWORDS, confidence_emoji, is_confirmed

# Pin PyTorch's CPU thread budget so it doesn't over-subscribe the container's
# vCPUs and trip Cloud Run's throttle heuristic (CLOUD_RUN_CPU_THROTTLE_FIX.md,
# Part 5). Default 2 matches the deploy's --cpu=2; override via OMP_NUM_THREADS.
# Guarded: set_num_interop_threads must run before any parallel work and raises
# if called twice — harmless to skip if torch was already engaged.
try:
    torch.set_num_threads(int(os.environ.get("OMP_NUM_THREADS", "2")))
    torch.set_num_interop_threads(1)
except Exception as _exc:   # pragma: no cover
    print(f">>> CLIP: thread pin skipped ({_exc})", flush=True)

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
_init_lock   = threading.Lock()

_era_means: dict | None = None   # era_label -> unit [D] tensor, built lazily

# (year, normalized_slogan) -> sloganID, loaded from the shared text_db.json so
# the Gemini pipeline can stage confirmed crops under reference/_staging/<id>/
# exactly where buttonmatcher's /reference flow consumes them. Best-effort: a
# (year, slogan) with no id falls back to the year-only "_year_YYYY" convention.
_slogan_key_to_entry: dict = {}

# tokenize / STOPWORDS / rarity_weight / confidence_emoji / is_confirmed are
# imported from scoring.py (pure-python, unit-testable). init() populates the
# scoring.word_freq table once the slogan set is loaded.


def init(bucket_name: str = config.BUCKET_NAME) -> None:
    """
    Load CLIP model and GCS vector files into module-level state.
    Must be called once before match_crop(). Thread-safe: concurrent callers
    block until the first completes, then return immediately.
    """
    global _model, _preprocess, _device
    global _ref_vectors, _ref_labels
    global _text_features, _text_phrases, _text_years, _text_types
    global _initialized

    with _init_lock:
        if _initialized:
            return

        print(">>> CLIP: Loading ViT-B/32 model...", flush=True)
        _model, _preprocess = clip.load("ViT-B/32", device=_device)
        print(">>> CLIP: Model loaded.", flush=True)

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

            # --- text_db.json (sloganID map for reference staging) ---
            # Optional: only used by the Gemini-pipeline staging path. Shared
            # schema: {"<id>": {"slogan":..., "year":..., "type":...}, ...}.
            _slogan_key_to_entry.clear()
            try:
                tdb_path = os.path.join(tmpdir, "text_db.json")
                bucket.blob("text_db.json").download_to_filename(tdb_path)
                import json as _json
                with open(tdb_path) as _fh:
                    _tdb = _json.load(_fh)
                for _eid, _rec in (_tdb or {}).items():
                    try:
                        _yr = int(_rec.get("year"))
                    except (TypeError, ValueError):
                        continue
                    _key = (_yr, normalize.normalize_key(_rec.get("slogan", "")))
                    _slogan_key_to_entry[_key] = str(_eid)
                print(f">>> CLIP: text_db.json loaded — {len(_slogan_key_to_entry)} entry ids.",
                      flush=True)
            except Exception as _exc:
                print(f">>> CLIP: text_db.json not loaded (staging will use _year_YYYY): {_exc}",
                      flush=True)

        # Build the rarity word-frequency table now that slogans are loaded
        # (buttonmatcher/main.py:680-685): freq = # of distinct slogans a word
        # appears in, so 1/freq² gives rare words a small tiebreaker boost.
        scoring.word_freq.clear()
        for phrase in _text_phrases:
            for word in set(tokenize(phrase)):
                scoring.word_freq[word] += 1
        print(f">>> CLIP: word_freq built — {len(scoring.word_freq)} unique words.", flush=True)

        _initialized = True
        print(">>> CLIP: Initialization complete.", flush=True)


def reference_years() -> set[int]:
    """Years present in the loaded text reference data (empty before init())."""
    return {int(y) for y in _text_years}


def text_db_arrays() -> tuple[list[str], list[int], list[str]]:
    """(_text_phrases, _text_years, _text_types) — the parallel slogan DB arrays.
    Used to build the Gemini resolver's slogan→years multimap (empty before init)."""
    return list(_text_phrases), [int(y) for y in _text_years], list(_text_types)


def entry_id_for(year, slogan: str) -> str:
    """Resolve the reference sloganID for (year, slogan) for crop staging.

    Returns the shared text_db.json sloganID when known, else the year-only
    "_year_YYYY" convention buttonmatcher also accepts (so a crop is never lost
    just because its exact slogan isn't ID-mapped)."""
    try:
        yr = int(str(year).split()[0])
    except (ValueError, IndexError, AttributeError):
        yr = None
    if yr is not None:
        eid = _slogan_key_to_entry.get((yr, normalize.normalize_key(slogan)))
        if eid:
            return eid
        return f"_year_{yr}"
    return "_year_unknown"


def _label_year(label) -> int | None:
    """Parse a year from a reference label (plain int year or 'YEAR SLOGAN')."""
    try:
        return int(label) if isinstance(label, (int, float)) else int(str(label).split()[0])
    except (ValueError, IndexError, AttributeError):
        return None


def _build_era_means() -> dict:
    """
    Centroid (unit) image embedding per era, from the reference vectors grouped
    by their year. Computed once; used to classify a crop's likely era.
    """
    means: dict = {}
    if _ref_vectors is None or not _ref_labels:
        return means
    years = np.array([(_label_year(l) if _label_year(l) is not None else -1)
                      for l in _ref_labels])
    for era, (lo, hi) in config.BUTTON_ERAS.items():
        idx = np.where((years >= lo) & (years <= hi))[0]
        if len(idx) == 0:
            continue
        vec = _ref_vectors[idx].mean(dim=0)
        vec = vec / vec.norm()
        means[era] = vec
    return means


def guess_lot_era(pil_images: list, sample_limit: int | None = None) -> tuple:
    """
    Guess the dominant era of a lot by classifying a sample of crops against
    per-era centroid embeddings and majority-voting.

    Returns (era_label_or_None, detail) where `detail` is a rich dict for logging:
    {guess, votes, sampled, total, per_crop:[{pick, scores:{era: cos}}]}.
    Era detection is a heuristic suggestion (see config.BUTTON_ERAS) — log it,
    don't trust it blindly.
    """
    global _era_means
    if not config.ENABLE_ERA_DETECTION:
        return None, {"enabled": False}
    if not _initialized:
        raise RuntimeError("clip_matcher.init() must be called before guess_lot_era().")
    if _era_means is None:
        _era_means = _build_era_means()
    if not _era_means or not pil_images:
        return None, {"guess": None, "votes": {}, "sampled": 0, "total": len(pil_images),
                      "per_crop": [], "eras": list((_era_means or {}).keys())}

    if sample_limit is None:
        sample_limit = config.ERA_SAMPLE_LIMIT
    sample = pil_images[:max(1, sample_limit)]

    tensors = torch.stack([_preprocess(img) for img in sample]).to(_device)
    with torch.inference_mode():
        vecs = _model.encode_image(tensors).float()
    vecs = vecs / vecs.norm(dim=-1, keepdim=True)

    era_labels = list(_era_means.keys())
    means_mat  = torch.stack([_era_means[e] for e in era_labels])   # [E, D]
    sims = (vecs @ means_mat.T).cpu().numpy()                       # [n, E]

    votes: dict = {}
    per_crop: list = []
    for i in range(sims.shape[0]):
        scores = {era_labels[j]: round(float(sims[i][j]), 4) for j in range(len(era_labels))}
        pick = max(scores, key=scores.get)
        votes[pick] = votes.get(pick, 0) + 1
        per_crop.append({"pick": pick, "scores": scores})

    guess = max(votes, key=votes.get) if votes else None
    return guess, {"guess": guess, "votes": votes, "sampled": len(sample),
                   "total": len(pil_images), "per_crop": per_crop}


def match_crops_batch(
    pil_images: list[Image.Image],
    threshold: float | None = None,
    top_k: int = 1,
    restrict_years: set[int] | None = None,
) -> list:
    """
    Match a list of PIL.Image crops in a single model forward pass.

    Dramatically faster than calling match_crop() in a loop — a batch of
    N crops costs roughly the same as a batch of 1 on CPU.

    top_k == 1 (default): returns one result per input image — the best match
    dict, or None where its score < threshold. Backwards compatible.

    top_k > 1: returns, per input image, a list of up to top_k match dicts
    (best first) whose score >= threshold. Used by the needed-button scan to
    inspect 2nd/3rd guesses on blended multi-button photos. The list is empty
    when no candidate clears the threshold.

    restrict_years: when set, only those years are considered as match
    candidates — used when the year is already known (from the search query or
    a single title year) so scoring collapses to "best slogan within that year".
    """
    if threshold is None:
        threshold = config.CONFIDENCE_THRESHOLD
    if not _initialized:
        raise RuntimeError("clip_matcher.init() must be called before match_crops_batch().")
    if not pil_images:
        return []

    # Encode in fixed-size chunks so an uncapped dense lot (100+ crops) doesn't
    # spike activation memory on a 4Gi container. Per-crop scoring is identical;
    # output order is preserved (chunks in order, crops within a chunk in order).
    batch = max(1, getattr(config, "ENCODE_BATCH", 16))
    results = []
    for start in range(0, len(pil_images), batch):
        chunk = pil_images[start:start + batch]
        tensors = torch.stack([_preprocess(img) for img in chunk]).to(_device)   # [n, C, H, W]
        with torch.inference_mode():
            vecs = _model.encode_image(tensors).float()                          # [n, D]
        vecs = vecs / vecs.norm(dim=-1, keepdim=True)

        for vec in vecs:
            vec = vec.unsqueeze(0)  # [1, D] — reuse single-crop scoring logic
            image_sims = (vec @ _ref_vectors.T).cpu().numpy()[0]
            text_sims  = (vec @ _text_features.T).cpu().numpy()[0]
            if top_k == 1:
                results.append(_score_best_match(image_sims, text_sims, threshold, restrict_years))
            else:
                results.append(_score_top_matches(image_sims, text_sims, threshold, top_k, restrict_years))
        del tensors, vecs   # release activations before the next chunk
    return results


def match_crop(
    pil_image: Image.Image,
    threshold: float | None = None,
    restrict_years: set[int] | None = None,
) -> dict | None:
    """Match a single crop. Prefer match_crops_batch() when processing a list."""
    results = match_crops_batch([pil_image], threshold, restrict_years=restrict_years)
    return results[0] if results else None


def _year_image_scores_all(image_sims: np.ndarray) -> dict[str, float]:
    """Per-year best image similarity over ALL reference years (str keys), for
    the shadow (unrestricted) leaderboard. build_leaderboard keys years by str."""
    scores: dict[str, float] = {}
    for i, label in enumerate(_ref_labels):
        y = _label_year(label)
        if y is None:
            continue
        ys = str(y)
        s  = float(image_sims[i])
        if ys not in scores or s > scores[ys]:
            scores[ys] = s
    return scores


def _crop_leaderboard(
    text_sims: np.ndarray,
    year_scores_str: dict[str, float],
    restrict_years: set[int] | None,
) -> list[dict]:
    """Full ranked leaderboard for one crop via match_logging.build_leaderboard,
    using the SAME normalize/tokenize/rarity/stopwords as the live scorer so the
    logged leaderboard scores equal the live ones."""
    allowed = {str(y) for y in restrict_years} if restrict_years else None
    return match_logging.build_leaderboard(
        text_sims, year_scores_str,
        _text_years, _text_phrases, _text_types,
        normalize_fn=_normalize_slogan, tokenize_fn=tokenize,
        rarity_fn=rarity_weight, stopwords=STOPWORDS,
        allowed_years=allowed, top_n=None,
    )


def match_crops_with_diagnostics(
    pil_images: list[Image.Image],
    restrict_years: set[int] | None = None,
    shadow: bool | None = None,
) -> list[dict]:
    """Match crops AND produce the per-crop logging payload in ONE encode pass.

    Returns one dict per input crop (order preserved):
      {
        "candidates":     [up to 10 match dicts, best first], # live dual-signal result
        "gap":            float | None,                       # #1.overall - #2.overall
        "restricted_top": [top-10 restricted leaderboard],    # match_log restricted_top
        "shadow_top":     [top-10 unrestricted leaderboard],  # match_log shadow_top
        "shadow_full":    [full unrestricted ranking],        # for match_logging.rank_of
        "shadow_enabled": bool,
      }

    No threshold is applied to ``candidates`` — every crop reports its best
    matches; the caller gates "confirmed" via is_confirmed(overall, gap). The
    shadow (all-years) leaderboard is skipped when BUTTONMATCHER_SHADOW_PASS=0.
    """
    if not _initialized:
        raise RuntimeError("clip_matcher.init() must be called before match_crops_with_diagnostics().")
    if not pil_images:
        return []
    if shadow is None:
        shadow = match_logging.shadow_pass_enabled()

    batch = max(1, getattr(config, "ENCODE_BATCH", 16))
    out: list[dict] = []
    for start in range(0, len(pil_images), batch):
        chunk   = pil_images[start:start + batch]
        tensors = torch.stack([_preprocess(img) for img in chunk]).to(_device)
        with torch.inference_mode():
            vecs = _model.encode_image(tensors).float()
        vecs = vecs / vecs.norm(dim=-1, keepdim=True)

        for vec in vecs:
            vec        = vec.unsqueeze(0)
            image_sims = (vec @ _ref_vectors.T).cpu().numpy()[0]
            text_sims  = (vec @ _text_features.T).cpu().numpy()[0]

            # _ranked_matches applies the rerank (env-gated) + the always-on
            # ref-photo check, so candidates already carry the refined ranking.
            ranked     = _ranked_matches(image_sims, text_sims, restrict_years)
            candidates = [_format_match(r) for r in ranked]
            gap = (candidates[0]["overall"] - candidates[1]["overall"]
                   if len(candidates) >= 2 else None)

            year_img_all   = _year_image_scores_all(image_sims)
            restricted_top = _crop_leaderboard(text_sims, year_img_all, restrict_years)
            shadow_full    = _crop_leaderboard(text_sims, year_img_all, None) if shadow else []

            out.append({
                "candidates":     candidates,
                "gap":            gap,
                "restricted_top": match_logging.trim_top(restricted_top, 10),
                "shadow_top":     match_logging.trim_top(shadow_full, 10),
                "shadow_full":    shadow_full,
                "shadow_enabled": shadow,
            })
        del tensors, vecs   # release activations before the next chunk
    return out


def _era_years_for(year: int) -> set[int]:
    """Years sharing `year`'s bank era (config.BUTTON_ERAS); {} if none."""
    out: set[int] = set()
    for _lo, _hi in config.BUTTON_ERAS.values():
        if _lo <= year <= _hi:
            out |= {int(y) for y in _text_years if _lo <= int(y) <= _hi}
    return out


def _apply_reference_rerank(ranked: list[dict], image_sims: np.ndarray) -> list[dict]:
    """Opt-in two-level reference re-rank (rerank.py) — Year Score + SloganID
    Score from the crop's similarities to the reference photos. Bounded ±0.05
    each; fail-open (returns `ranked` unchanged on any error). Off by default."""
    try:
        # Per-entry (year, normalized-slogan) max crop↔ref similarity, and per-year.
        entry_sims: dict[tuple, float] = {}
        year_max_sims: dict[int, float] = {}
        entry_phrases: list[str] = []
        entry_years: list[int] = []
        for i, label in enumerate(_ref_labels):
            yr = _label_year(label)
            if yr is None:
                continue
            s = float(image_sims[i])
            if yr not in year_max_sims or s > year_max_sims[yr]:
                year_max_sims[yr] = s
            # entry key from the label's slogan text (label = "YEAR SLOGAN...")
            parts = str(label).split(None, 1)
            phrase = parts[1] if len(parts) > 1 else ""
            key = (yr, normalize.normalize_key(phrase))
            if key not in entry_sims:
                entry_sims[key] = s
                entry_phrases.append(phrase)
                entry_years.append(yr)
            elif s > entry_sims[key]:
                entry_sims[key] = s

        entry_keys = list(entry_sims.keys())
        entry_sim_list = [entry_sims[k] for k in entry_keys]

        def _score_fn(result):
            yr = result.get("year")
            try:
                yr = int(str(yr).split()[0])
            except (ValueError, IndexError, AttributeError):
                return 0.0, 0.0
            ys = rerank.year_score(yr, year_max_sims, _era_years_for(yr))
            ekey = (yr, normalize.normalize_key(result.get("slogan", "")))
            esim = entry_sims.get(ekey)
            peers = rerank.similar_slogan_peers(
                result.get("slogan", ""), yr, entry_phrases, entry_years,
                tokenize, STOPWORDS)
            peer_sims = [entry_sim_list[p] for p in peers]
            ss = rerank.sloganid_score(esim, peer_sims)
            return ys, ss

        return rerank.rerank_results(ranked, _score_fn)
    except Exception as exc:   # pragma: no cover
        print(f">>> CLIP: reference rerank skipped ({exc})", flush=True)
        return ranked


def _apply_ref_photo_check(ranked: list[dict], image_sims: np.ndarray) -> list[dict]:
    """Always-on entry-level reference-photo visual check (buttonmatcher's
    REF_CHECK step, buttonmatcher/main.py:1631-1671). For each candidate, take
    the crop's max similarity to THAT (year, slogan) entry's reference photos and
    nudge `overall += REF_CHECK_WEIGHT * ref_sim`, then re-sort. Entries with no
    matching reference photos are left untouched (no bonus, no penalty).
    Fail-open: returns `ranked` unchanged on any error."""
    try:
        # Per-entry (year, normalized-slogan) max crop↔ref similarity.
        entry_sims: dict[tuple, float] = {}
        for i, label in enumerate(_ref_labels):
            yr = _label_year(label)
            if yr is None:
                continue
            parts  = str(label).split(None, 1)          # label = "YEAR SLOGAN..."
            phrase = parts[1] if len(parts) > 1 else ""
            key    = (yr, normalize.normalize_key(phrase))
            s      = float(image_sims[i])
            if key not in entry_sims or s > entry_sims[key]:
                entry_sims[key] = s

        any_ref = False
        for r in ranked:
            try:
                yr = int(str(r.get("year")).split()[0])
            except (ValueError, IndexError, AttributeError):
                r["ref_sim"] = None
                continue
            esim = entry_sims.get((yr, normalize.normalize_key(r.get("slogan", ""))))
            r["ref_sim"] = esim
            if esim is not None:
                r["overall"] = min(1.0, r["overall"] + config.REF_CHECK_WEIGHT * esim)
                any_ref = True
        if any_ref:
            ranked.sort(key=lambda x: (x["overall"], x.get("slogan_score", 0)),
                        reverse=True)
        return ranked
    except Exception as exc:   # pragma: no cover
        print(f">>> CLIP: ref-photo check skipped ({exc})", flush=True)
        return ranked


def _ranked_matches(
    image_sims: np.ndarray,
    text_sims:  np.ndarray,
    restrict_years: set[int] | None = None,
) -> list[dict]:
    """
    Rank candidate (year, slogan) matches for one encoded crop, best first.
    Returns the raw _score_slogans output (year as int); callers apply the
    threshold and format. Empty list if no candidate years could be scored.

    restrict_years: when set, only those years are considered (the year is
    already known from the search query or a single title year).
    """

    # Build year → best image score map from reference vectors
    year_image_scores: dict[int, float] = {}
    for i, label in enumerate(_ref_labels):
        try:
            year = int(label) if isinstance(label, (int, float)) else int(str(label).split()[0])
        except (ValueError, IndexError, AttributeError):
            continue
        if restrict_years is not None and year not in restrict_years:
            continue
        score = float(image_sims[i])
        if year not in year_image_scores or score > year_image_scores[year]:
            year_image_scores[year] = score

    if not year_image_scores:
        return []

    # Build year → best raw text similarity over ALL text years (restricted),
    # independent of the image-year set, so the text signal can vote in a year
    # the image signal missed (buttonmatcher's Signal 2).
    years_arr = np.array(_text_years, dtype=np.int32)
    year_text_best: dict[int, float] = {}
    for year in set(int(y) for y in _text_years):
        if restrict_years is not None and year not in restrict_years:
            continue
        mask = years_arr == year
        if mask.any():
            year_text_best[year] = float(text_sims[mask].max())

    # --- Dual-signal year selection (buttonmatcher/main.py:1539-1565) ---
    # Signal 1: top-5 years by image similarity (visual match to reference photos).
    top_image_years = dict(
        sorted(year_image_scores.items(), key=lambda x: x[1], reverse=True)[:5]
    )
    # Signal 2: top-5 years by max text similarity (CLIP image-vs-slogan text) —
    # rescues near-identical visual templates where the slogan discriminates.
    top_text_years = dict(
        sorted(year_text_best.items(), key=lambda x: x[1], reverse=True)[:5]
    )
    # Merge: image years first, then text years not already present, capped at 8.
    # Wider than the old 3+3/6 union so the rerank's Year/SloganID scores and the
    # ref-photo check have the right year present to promote (and so a Gemini
    # slogan can match a lower-ranked-but-correct candidate). The score_slogans
    # loop over ≤8 entries costs nothing.
    top_year_image_scores = dict(top_image_years)
    for year in top_text_years:
        if len(top_year_image_scores) >= 8:
            break
        if year not in top_year_image_scores:
            top_year_image_scores[year] = year_image_scores.get(year, 0.0)

    allowed_years = set(top_year_image_scores.keys())

    # Score slogans for each candidate year
    ranked = _score_slogans(
        text_sims=np.array(text_sims, dtype=np.float32),
        year_scores=top_year_image_scores,
        allowed_years=allowed_years,
    )
    if not ranked:
        return ranked

    # Two-level reference refinement, in buttonmatcher's order:
    #   1) opt-in rerank (Year + SloganID deltas, env-gated, default off)
    #   2) ALWAYS-ON entry-level reference-photo visual check (REF_CHECK)
    # Both fail open. Applied here (the single ranking path) so every caller —
    # the daily scan, /scout, and the Gemini pipeline — sees the same ranking.
    if rerank.rerank_enabled():
        ranked = _apply_reference_rerank(ranked, image_sims)
    ranked = _apply_ref_photo_check(ranked, image_sims)
    return ranked


def _format_match(result: dict) -> dict:
    """Normalise a _score_slogans result into the public match dict shape."""
    return {
        "year":         str(result["year"]),
        "slogan":       result["slogan"],
        "overall":      float(result["overall"]),
        "image_score":  float(result["image_score"]),
        "slogan_score": float(result["slogan_score"]),
    }


def _score_best_match(
    image_sims: np.ndarray,
    text_sims:  np.ndarray,
    threshold:  float,
    restrict_years: set[int] | None = None,
) -> dict | None:
    """Score one encoded crop and return the best match dict, or None."""
    results = _ranked_matches(image_sims, text_sims, restrict_years)
    if not results or results[0]["overall"] < threshold:
        return None
    return _format_match(results[0])


def _score_top_matches(
    image_sims: np.ndarray,
    text_sims:  np.ndarray,
    threshold:  float,
    top_k:      int,
    restrict_years: set[int] | None = None,
) -> list[dict]:
    """Score one encoded crop and return up to top_k match dicts >= threshold."""
    results = _ranked_matches(image_sims, text_sims, restrict_years)
    return [
        _format_match(r) for r in results[:top_k]
        if r["overall"] >= threshold
    ]


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
    Returns results sorted by (overall, slogan_score) descending, top 10.
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

        # Boost for near-certain text match (>90%) — ramps fast. Single tier only,
        # matching buttonmatcher score_slogans / build_leaderboard (no 75–90% tier).
        if slogan_score > 0.9:
            overall += (slogan_score - 0.9) * 2.5
        # Penalty for very weak text match
        if slogan_score < config.SLOGAN_PENALTY_THRESHOLD:
            overall *= config.PENALTY_MULTIPLIER

        # Rarity tiebreaker (capped at +0.04): rewards distinctive slogan words so
        # a rare exact phrase edges out a generic one. Mirrors build_leaderboard
        # EXACTLY so logged leaderboards equal live scores.
        _words = set(tokenize(best_phrase)) - STOPWORDS
        if _words:
            _bonus  = min(0.04 * sum(rarity_weight(w) for w in _words) / len(_words), 0.04)
            overall = min(1.0, overall + _bonus)

        results.append({
            "year":         year,
            "slogan":       best_phrase,
            "overall":      overall,
            "image_score":  image_score,
            "slogan_score": slogan_score,
        })

    results.sort(key=lambda x: (x["overall"], x["slogan_score"]), reverse=True)
    # Keep up to 10 (buttonmatcher scores with limit=10). The reference rerank /
    # ref-photo check re-sort this list, and the Gemini resolver matches its
    # slogan against the full top-10 — so do NOT trim to 3 here.
    return results[:10]


def _normalize_slogan(score: float, min_s: float = 0.15, max_s: float = 0.35) -> float:
    """Normalise raw CLIP cosine similarity into [0, 1]. Identical to both services."""
    norm = (score - min_s) / (max_s - min_s)
    return max(0.0, min(1.0, norm))
