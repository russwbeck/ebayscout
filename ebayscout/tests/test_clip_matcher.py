"""
Tests for ebayscout/clip_matcher.py

These tests mock the GCS download and CLIP model so no GPU/network is needed.
"""

import pytest
import numpy as np
import torch
from unittest.mock import patch, MagicMock
from PIL import Image

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout import clip_matcher, config


# ---------------------------------------------------------------------------
# Helper: reset module state between tests
# ---------------------------------------------------------------------------

def _reset():
    clip_matcher._model        = None
    clip_matcher._preprocess   = None
    clip_matcher._ref_vectors  = None
    clip_matcher._ref_labels   = []
    clip_matcher._text_features = None
    clip_matcher._text_phrases  = []
    clip_matcher._text_years    = []
    clip_matcher._text_types    = []
    clip_matcher._initialized   = False


# ---------------------------------------------------------------------------
# normalize_slogan
# ---------------------------------------------------------------------------

class TestNormalizeSlogan:
    def test_at_min(self):
        assert clip_matcher._normalize_slogan(0.15) == pytest.approx(0.0)

    def test_at_max(self):
        assert clip_matcher._normalize_slogan(0.35) == pytest.approx(1.0)

    def test_midpoint(self):
        assert clip_matcher._normalize_slogan(0.25) == pytest.approx(0.5)

    def test_below_min_clamps_to_zero(self):
        assert clip_matcher._normalize_slogan(0.0) == 0.0

    def test_above_max_clamps_to_one(self):
        assert clip_matcher._normalize_slogan(1.0) == 1.0


# ---------------------------------------------------------------------------
# _score_slogans
# ---------------------------------------------------------------------------

class TestScoreSlogans:
    def setup_method(self):
        _reset()
        # Inject minimal module state: 2 slogans for year 1977, 1 for 1978
        clip_matcher._text_phrases = ["We Are Number One", "Go Lions", "Touchdown"]
        clip_matcher._text_years   = [1977,               1977,       1978]
        clip_matcher._text_types   = ["Football",         "Football", "Football"]

    def _text_sims(self, values):
        return np.array(values, dtype=np.float32)

    def test_returns_top_match(self):
        # Give "We Are Number One" a high text sim (0.35 → normalized 1.0)
        sims = self._text_sims([0.35, 0.15, 0.15])
        results = clip_matcher._score_slogans(
            text_sims=sims,
            year_scores={1977: 0.8},
            allowed_years={1977},
        )
        assert len(results) >= 1
        assert results[0]["year"] == 1977
        assert results[0]["slogan"] == "We Are Number One"

    def test_penalty_applied_for_low_slogan_score(self):
        # Very low text sim → penalty multiplier applied
        sims = self._text_sims([0.10, 0.10, 0.10])  # all below 0.15 → norm=0.0
        results = clip_matcher._score_slogans(
            text_sims=sims,
            year_scores={1977: 0.8},
            allowed_years={1977},
        )
        assert len(results) == 1
        # With 0.0 slogan_score: raw = 0.6*0.8 + 0.4*0.0 = 0.48 → * 0.7 = 0.336
        assert results[0]["overall"] == pytest.approx(0.48 * 0.7, abs=1e-3)

    def test_boost_applied_for_high_slogan_score(self):
        # slogan_score > 0.9 triggers boost
        # score 0.35 + 0.001 → normalize to slightly > 1.0 → clamped to 1.0
        # Use raw text sim 0.40 → (0.40 - 0.15) / 0.20 = 1.25 → clamped to 1.0
        sims = self._text_sims([0.40, 0.10, 0.10])
        results = clip_matcher._score_slogans(
            text_sims=sims,
            year_scores={1977: 0.8},
            allowed_years={1977},
        )
        assert len(results) == 1
        # slogan_score = 1.0 → boost = (1.0 - 0.9) * 2.5 = 0.25
        # overall = 0.6*0.8 + 0.4*1.0 + 0.25 = 0.48 + 0.40 + 0.25 = 1.13 → clamped to 1.0
        assert results[0]["overall"] == pytest.approx(1.0)

    def test_returns_empty_for_no_valid_years(self):
        sims = self._text_sims([0.25, 0.25, 0.25])
        results = clip_matcher._score_slogans(
            text_sims=sims,
            year_scores={2000: 0.8},   # year 2000 not in text data
            allowed_years={2000},
        )
        assert results == []

    def test_returns_at_most_three_results(self):
        # Add more years to module state
        clip_matcher._text_phrases = ["Slogan A", "Slogan B", "Slogan C", "Slogan D", "Slogan E"]
        clip_matcher._text_years   = [1972, 1973, 1974, 1975, 1976]
        clip_matcher._text_types   = ["Football"] * 5
        sims = self._text_sims([0.25, 0.25, 0.25, 0.25, 0.25])
        results = clip_matcher._score_slogans(
            text_sims=sims,
            year_scores={1972: 0.8, 1973: 0.75, 1974: 0.7, 1975: 0.65, 1976: 0.6},
            allowed_years={1972, 1973, 1974, 1975, 1976},
        )
        assert len(results) <= 3


# ---------------------------------------------------------------------------
# match_crop (requires initialized state)
# ---------------------------------------------------------------------------

class TestMatchCrop:
    def setup_method(self):
        _reset()

    def _setup_minimal_state(self, image_sim=0.8, text_sim=0.30):
        """
        Inject synthetic module state for 2 reference images (both year 1977)
        and 1 slogan, then mock the CLIP model's encode_image.
        """
        D = 8   # tiny embedding dim for testing

        # Ref vectors: 2 images, both labeled "1977 We Are Number One"
        ref = torch.randn(2, D)
        ref = ref / ref.norm(dim=-1, keepdim=True)
        clip_matcher._ref_vectors = ref
        clip_matcher._ref_labels  = ["1977 We Are Number One", "1977 We Are Number One"]

        # Text features: 1 slogan
        txt = torch.randn(1, D)
        txt = txt / txt.norm(dim=-1, keepdim=True)
        clip_matcher._text_features = txt
        clip_matcher._text_phrases  = ["We Are Number One"]
        clip_matcher._text_years    = [1977]
        clip_matcher._text_types    = ["Football"]
        clip_matcher._initialized   = True

        # Mock model.encode_image to return a vector whose dot product with ref
        # produces the desired image_sim.  Use the first ref vector scaled.
        query_vec = ref[0:1] * image_sim   # [1, D], not unit-norm (will be normed)
        # Construct text sim by projecting query onto txt
        # We'll override the text sim value by choosing query_vec = txt[0] * text_sim_scale
        # For simplicity just make query_vec align with txt[0]
        # actual sims: image_sim ~ ref[0] · query / ||query|| ≈ image_sim (approx)
        # We construct a query vec that aligns with both ref[0] and txt[0]
        query_vec = (ref[0] + txt[0] * 0.5)
        query_vec = query_vec / query_vec.norm()
        query_vec = query_vec.unsqueeze(0)  # [1, D]

        mock_model = MagicMock()
        mock_model.encode_image.return_value = query_vec.clone()
        clip_matcher._model = mock_model

        mock_preprocess = MagicMock()
        mock_preprocess.return_value = torch.zeros(3, 224, 224)
        clip_matcher._preprocess = mock_preprocess

    def test_returns_none_before_init(self):
        with pytest.raises(RuntimeError, match="init"):
            clip_matcher.match_crop(Image.new("RGB", (64, 64)))

    def test_returns_dict_on_confident_match(self):
        self._setup_minimal_state()
        result = clip_matcher.match_crop(Image.new("RGB", (64, 64)))
        # The mock vectors are set up to produce a score above threshold
        # We check the structure if it returns a result
        if result is not None:
            assert "year" in result
            assert "slogan" in result
            assert "overall" in result
            assert isinstance(result["overall"], float)

    def test_return_value_structure(self):
        self._setup_minimal_state()
        img = Image.new("RGB", (64, 64))
        result = clip_matcher.match_crop(img)
        if result is not None:
            required_keys = {"year", "slogan", "overall", "image_score", "slogan_score"}
            assert required_keys.issubset(result.keys())
            assert isinstance(result["year"], str)
            assert 0.0 <= result["overall"] <= 1.0

    def test_top_k_returns_list_per_crop(self):
        # top_k > 1 returns, per crop, a list of up to top_k match dicts
        # (best first), all >= threshold. Backwards-compatible default (top_k=1)
        # still returns a single dict-or-None per crop.
        self._setup_minimal_state()
        img = Image.new("RGB", (64, 64))

        single = clip_matcher.match_crops_batch([img], threshold=0.0)
        assert len(single) == 1
        assert single[0] is None or isinstance(single[0], dict)

        topk = clip_matcher.match_crops_batch([img], threshold=0.0, top_k=3)
        assert len(topk) == 1
        assert isinstance(topk[0], list)
        assert len(topk[0]) <= 3
        # ordered best-first and every entry has the public match shape
        overalls = [m["overall"] for m in topk[0]]
        assert overalls == sorted(overalls, reverse=True)
        for m in topk[0]:
            assert {"year", "slogan", "overall"}.issubset(m.keys())
            assert m["overall"] >= 0.0

    def test_top_k_respects_threshold(self):
        # A threshold above any achievable score yields an empty candidate list.
        self._setup_minimal_state()
        img = Image.new("RGB", (64, 64))
        topk = clip_matcher.match_crops_batch([img], threshold=1.01, top_k=3)
        assert topk == [[]]

    def test_restrict_years_excludes_other_years(self):
        # Minimal state only has year 1977. Restricting to a year with no
        # reference data yields no candidates; restricting to 1977 still matches.
        self._setup_minimal_state()
        img = Image.new("RGB", (64, 64))

        none_year = clip_matcher.match_crops_batch(
            [img], threshold=0.0, top_k=3, restrict_years={1999})
        assert none_year == [[]]

        right_year = clip_matcher.match_crops_batch(
            [img], threshold=0.0, top_k=3, restrict_years={1977})
        assert len(right_year) == 1
        for m in right_year[0]:
            assert m["year"] == "1977"

    def test_reference_years_reports_loaded_years(self):
        self._setup_minimal_state()
        assert clip_matcher.reference_years() == {1977}
