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


class TestSendWarning:
    def test_posts_warning_message(self):
        mock_client = MagicMock()
        with patch("ebayscout.notifier.WebClient", return_value=mock_client):
            notifier.send_warning("xoxb-fake", "#scout", "Test warning message")
        mock_client.chat_postMessage.assert_called_once()
        call_text = mock_client.chat_postMessage.call_args[1].get("text", "")
        assert "Test warning message" in call_text
