"""
Tests for ebayscout/notifier.py
"""

import pytest
from unittest.mock import patch, MagicMock

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout import notifier


FAKE_LISTING = {
    "item_id":       "123456",
    "title":         "Lot of 12 Penn State Football Buttons 1970s-1980s",
    "listing_url":   "https://www.ebay.com/itm/123456",
    "seller":        "sporty_collector",
    "current_price": 15.00,
}

FAKE_MATCHES = [
    {"year": "1977", "slogan": "We Are Number One", "overall": 0.88,
     "max_price_single": "$4.00", "amount_needed": 2},
    {"year": "1980", "slogan": "4th & Goal",         "overall": 0.80,
     "max_price_single": "$3.50", "amount_needed": 0},
]


class TestSendUndervaluedAlert:
    def test_calls_chat_post_message_once(self):
        mock_client = MagicMock()
        with patch("ebayscout.notifier.WebClient", return_value=mock_client):
            notifier.send_undervalued_alert(
                slack_token="xoxb-fake",
                channel="#test",
                listing=FAKE_LISTING,
                matches=FAKE_MATCHES,
                lot_value=7.50,
                asking_price=15.00,
                margin=-7.50,   # undervalued check is done by caller
                unmatched_count=2,
            )
        mock_client.chat_postMessage.assert_called_once()

    def test_message_contains_key_fields(self):
        captured = {}

        def fake_post(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        mock_client = MagicMock()
        mock_client.chat_postMessage.side_effect = fake_post

        with patch("ebayscout.notifier.WebClient", return_value=mock_client):
            notifier.send_undervalued_alert(
                slack_token="xoxb-fake",
                channel="#test-channel",
                listing=FAKE_LISTING,
                matches=FAKE_MATCHES,
                lot_value=7.50,
                asking_price=15.00,
                margin=-7.50,
                unmatched_count=2,
            )

        text = captured.get("text", "")
        assert "$15.00" in text          # asking price
        assert "$7.50" in text           # lot value
        assert "1977" in text            # year
        assert "We Are Number One" in text
        assert "sporty_collector" in text
        assert "2 additional" in text    # unmatched count


class TestSendNeededAlert:
    def test_calls_chat_post_message_once(self):
        needed = [m for m in FAKE_MATCHES if m["amount_needed"] > 0]
        mock_client = MagicMock()
        with patch("ebayscout.notifier.WebClient", return_value=mock_client):
            notifier.send_needed_alert(
                slack_token="xoxb-fake",
                channel="#test",
                listing=FAKE_LISTING,
                needed_buttons=needed,
                asking_price=15.00,
                lot_value=7.50,
            )
        mock_client.chat_postMessage.assert_called_once()

    def test_message_contains_needed_buttons(self):
        needed = [m for m in FAKE_MATCHES if m["amount_needed"] > 0]
        captured = {}

        mock_client = MagicMock()
        mock_client.chat_postMessage.side_effect = lambda **kw: captured.update(kw) or MagicMock()

        with patch("ebayscout.notifier.WebClient", return_value=mock_client):
            notifier.send_needed_alert(
                slack_token="xoxb-fake",
                channel="#test-channel",
                listing=FAKE_LISTING,
                needed_buttons=needed,
                asking_price=15.00,
                lot_value=7.50,
            )

        text = captured.get("text", "")
        assert "We Are Number One" in text
        assert "need 2" in text
        assert "sporty_collector" in text
        assert "#test-channel" == captured.get("channel")


class TestSendBackfillDigest:
    _RECORDS = [
        {"title": "Penn State 1977 button lot", "listing_url": "https://ebay.com/itm/1",
         "asking": 20.0, "needed_hit": True,
         "best_needed": {"year": "1977", "slogan": "We Are Number One", "overall": 0.71}},
        {"title": "PSU 2003 pin", "listing_url": "https://ebay.com/itm/2",
         "asking": 25.0, "needed_hit": False,
         "best_needed": {"year": "2003", "slogan": "Gopher Broke", "overall": 0.58}},
        {"title": "random hoodie", "listing_url": "", "asking": 5.0,
         "needed_hit": False, "best_needed": None},
    ]

    def _capture(self):
        captured = {}
        mock_client = MagicMock()
        mock_client.chat_postMessage.side_effect = lambda **kw: captured.update(kw) or MagicMock()
        return captured, mock_client

    def test_single_message_with_scores_and_counts(self):
        captured, mock_client = self._capture()
        with patch("ebayscout.notifier.WebClient", return_value=mock_client):
            notifier.send_backfill_digest("xoxb-fake", "#scout", self._RECORDS, threshold=0.60)
        mock_client.chat_postMessage.assert_called_once()
        text = captured.get("text", "")
        assert "DRY RUN" in text
        assert "0.71" in text and "0.58" in text     # both candidate scores shown
        assert "✅" in text and "▫️" in text          # above + below threshold marks
        assert "would alert on *1*" in text          # exactly one needed_hit
        # the non-needed (best_needed=None) listing is excluded from the score list
        assert "hoodie" not in text

    def test_handles_no_needed_candidates(self):
        captured, mock_client = self._capture()
        none_records = [{"title": "x", "best_needed": None, "needed_hit": False, "asking": 1.0}]
        with patch("ebayscout.notifier.WebClient", return_value=mock_client):
            notifier.send_backfill_digest("xoxb-fake", "#scout", none_records, threshold=0.60)
        text = captured.get("text", "")
        assert "No needed-button candidates" in text


class TestSendWarning:
    def test_posts_warning_message(self):
        mock_client = MagicMock()
        with patch("ebayscout.notifier.WebClient", return_value=mock_client):
            notifier.send_warning("xoxb-fake", "#scout", "Test warning message")
        mock_client.chat_postMessage.assert_called_once()
        call_text = mock_client.chat_postMessage.call_args[1].get("text", "")
        assert "Test warning message" in call_text


class TestLookalikeFlag:
    """The look-alike warning on a deal alert.

    ebayscout has no human-review lane to demote a confusable match into
    (`_post_yellow_review` is defined but never called), so instead of dropping
    the match it warns on the post — at the one moment a human IS present and
    money is at stake. The numbers are the point: a same-year look-alike keys a
    different `get_buy_decision` row, so it moves both what the lot is worth and
    whether the button is needed at all.
    """

    NOTE = {
        "alternatives": [{"slogan": "Penn State and Proud of it", "year": "1992",
                          "price": 12.0, "amount_needed": 0}],
        "price_here": 30.0,
        "value_swing": 18.0,
        "need_here": 1,
        "any_not_needed": True,
    }

    def test_line_names_the_sibling_its_value_and_the_swing(self):
        line = notifier._lookalike_line(self.NOTE)
        assert "Penn State and Proud of it" in line
        assert "$12.00" in line          # what it would be worth instead
        assert "$18.00" in line          # how far the lot value could be off
        assert "NOT needed" in line
        assert "check the photo" in line

    def test_no_line_without_a_note(self):
        assert notifier._lookalike_line(None) == ""
        assert notifier._lookalike_line({}) == ""
        assert notifier._lookalike_line({"alternatives": []}) == ""

    def test_no_swing_line_when_the_money_is_the_same(self):
        note = dict(self.NOTE, value_swing=0.0, any_not_needed=True)
        note["alternatives"] = [dict(note["alternatives"][0], price=30.0)]
        line = notifier._lookalike_line(note)
        assert "could be off by" not in line
        assert "check the photo" in line

    def test_needed_alert_carries_the_flag(self):
        mock_client = MagicMock()
        needed = [{"year": "1992", "slogan": "'Eers to Penn State",
                   "max_price_single": "$30.00", "amount_needed": 1,
                   "lookalike": self.NOTE}]
        with patch("ebayscout.notifier.WebClient", return_value=mock_client):
            notifier.send_needed_alert(
                slack_token="xoxb-fake", channel="#scout", listing=FAKE_LISTING,
                needed_buttons=needed, asking_price=15.0, lot_value=30.0)
        text = mock_client.chat_postMessage.call_args[1].get("text", "")
        assert "'Eers to Penn State" in text
        assert "look-alike" in text
        assert "$18.00" in text

    def test_needed_alert_unchanged_when_nothing_is_confusable(self):
        mock_client = MagicMock()
        needed = [{"year": "1992", "slogan": "Clear Winner",
                   "max_price_single": "$30.00", "amount_needed": 1,
                   "lookalike": None}]
        with patch("ebayscout.notifier.WebClient", return_value=mock_client):
            notifier.send_needed_alert(
                slack_token="xoxb-fake", channel="#scout", listing=FAKE_LISTING,
                needed_buttons=needed, asking_price=15.0, lot_value=30.0)
        text = mock_client.chat_postMessage.call_args[1].get("text", "")
        assert "look-alike" not in text
