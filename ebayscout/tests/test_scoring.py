"""
Tests for ebayscout/scoring.py — the pure-python confidence tiers and rarity
tiebreaker. No torch/cv2/GCS needed (scoring imports only config + stdlib).
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout import scoring, config


class TestIsConfirmed:
    def test_auto_confirm(self):
        assert scoring.is_confirmed(config.AUTO_RESOLVE_THRESHOLD) is True
        assert scoring.is_confirmed(0.99) is True

    def test_green_by_solo_score(self):
        assert scoring.is_confirmed(config.GREEN_THRESHOLD) is True
        assert scoring.is_confirmed(0.83) is True

    def test_green_by_gap_below_solo_threshold(self):
        # below GREEN_THRESHOLD but a decisive #1-vs-#2 lead → still green
        assert scoring.is_confirmed(0.70, gap=config.GREEN_GAP) is True
        assert scoring.is_confirmed(0.70, gap=0.20) is True

    def test_not_confirmed_yellow(self):
        assert scoring.is_confirmed(0.70, gap=0.05) is False
        assert scoring.is_confirmed(0.81, gap=None) is False

    def test_not_confirmed_red(self):
        assert scoring.is_confirmed(0.50) is False
        assert scoring.is_confirmed(0.0) is False


class TestConfidenceEmoji:
    def test_green_solo(self):
        assert scoring.confidence_emoji(0.90) == "🟢"
        assert scoring.confidence_emoji(config.GREEN_THRESHOLD) == "🟢"

    def test_green_by_gap(self):
        assert scoring.confidence_emoji(0.70, gap=0.20) == "🟢"

    def test_yellow(self):
        assert scoring.confidence_emoji(0.70, gap=0.0) == "🟡"
        assert scoring.confidence_emoji(0.66) == "🟡"

    def test_red(self):
        assert scoring.confidence_emoji(0.50) == "🔴"
        assert scoring.confidence_emoji(config.RED_THRESHOLD - 0.01) == "🔴"


class TestRarity:
    def setup_method(self):
        scoring.word_freq.clear()

    def test_rarity_weight_superlinear(self):
        scoring.word_freq.update({"bowling": 1, "lions": 2, "state": 5})
        assert scoring.rarity_weight("bowling") == 1.0
        assert scoring.rarity_weight("lions") == 1.0 / 4
        assert scoring.rarity_weight("state") == 1.0 / 25
        # Unknown word defaults to freq 1.
        assert scoring.rarity_weight("zzzz") == 1.0

    def test_rarity_bonus_capped(self):
        scoring.word_freq.update({"bowling": 1})
        assert 0 < scoring.rarity_bonus("Bowling") <= 0.04

    def test_rarity_bonus_zero_for_stopwords_only(self):
        assert scoring.rarity_bonus("the and or") == 0.0

    def test_tokenize(self):
        assert scoring.tokenize("We Are #1, Lions!") == ["we", "are", "1", "lions"]
