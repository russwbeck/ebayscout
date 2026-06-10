"""
Tests for ebayscout/gemini_triage.py — pure-Python where possible.

normalize_slogan / slogans_match and the fail-open path (real google-genai
isn't installed in this environment, so analyze_lot_with_gemini's import
naturally fails and falls back to EMPTY_RESULT) run anywhere. The
JSON-parsing happy path is tested by injecting fake google.genai/PIL modules.
"""

import json
import sys
import types
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout import gemini_triage


class TestNormalizeSlogan:
    def test_lowercases_and_strips_punctuation(self):
        assert gemini_triage.normalize_slogan("Stop Stanford!") == "stop stanford"

    def test_collapses_whitespace(self):
        assert gemini_triage.normalize_slogan("  Whip   the\nWolfpack ") == "whip the wolfpack"

    def test_none_and_empty(self):
        assert gemini_triage.normalize_slogan(None) == ""
        assert gemini_triage.normalize_slogan("") == ""


class TestSlogansMatch:
    def test_exact_match(self):
        assert gemini_triage.slogans_match("Stop Stanford", "Stop Stanford")

    def test_case_and_punctuation_insensitive(self):
        assert gemini_triage.slogans_match("Stop Stanford!", "stop  stanford")

    def test_no_match(self):
        assert not gemini_triage.slogans_match("Stop Stanford", "Whip the Wolfpack")


class TestAnalyzeLotWithGeminiFailOpen:
    def test_returns_empty_result_without_genai_installed(self):
        # google-genai isn't installed in this environment, so the lazy
        # `from google import genai` import raises and the function fails
        # open to EMPTY_RESULT — exercised here without any mocking.
        result = gemini_triage.analyze_lot_with_gemini(b"not-a-real-image", api_key="fake")
        assert result == gemini_triage.EMPTY_RESULT
        assert result is not gemini_triage.EMPTY_RESULT  # caller gets its own copy


class _FakeGenerateContentConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeResponse:
    def __init__(self, text):
        self.text = text


def _install_fake_genai(monkeypatch, response_text=None, raise_exc=None):
    """Inject fake google.genai / google.genai.types / PIL modules so
    analyze_lot_with_gemini's lazy imports succeed in this test."""

    class _FakeModels:
        def generate_content(self, model, contents, config):
            if raise_exc is not None:
                raise raise_exc
            return _FakeResponse(response_text)

    class _FakeClient:
        def __init__(self, api_key=None):
            self.api_key = api_key
            self.models = _FakeModels()

    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = _FakeClient

    fake_genai_types = types.ModuleType("google.genai.types")
    fake_genai_types.GenerateContentConfig = _FakeGenerateContentConfig
    fake_genai.types = fake_genai_types

    fake_google = types.ModuleType("google")
    fake_google.genai = fake_genai

    fake_pil_image_module = types.ModuleType("PIL.Image")
    fake_pil_image_module.open = lambda fp: "FAKE_IMAGE"

    fake_pil = types.ModuleType("PIL")
    fake_pil.Image = fake_pil_image_module

    for name, mod in {
        "google": fake_google,
        "google.genai": fake_genai,
        "google.genai.types": fake_genai_types,
        "PIL": fake_pil,
        "PIL.Image": fake_pil_image_module,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


class TestAnalyzeLotWithGeminiHappyPath:
    def test_parses_json_response(self, monkeypatch):
        payload = {
            "total_button_count": 11,
            "blue_background_count": 10,
            "white_background_count": 1,
            "detected_slogans": ["Stop Stanford", "Whip the Wolfpack"],
            "flagged_problem_slogans": ["???"],
        }
        _install_fake_genai(monkeypatch, response_text=json.dumps(payload))

        result = gemini_triage.analyze_lot_with_gemini(b"fake-bytes", api_key="fake")

        assert result == payload

    def test_fails_open_on_bad_json(self, monkeypatch):
        _install_fake_genai(monkeypatch, response_text="not json")

        result = gemini_triage.analyze_lot_with_gemini(b"fake-bytes", api_key="fake")

        assert result == gemini_triage.EMPTY_RESULT

    def test_fails_open_on_api_error(self, monkeypatch):
        _install_fake_genai(monkeypatch, raise_exc=RuntimeError("boom"))

        result = gemini_triage.analyze_lot_with_gemini(b"fake-bytes", api_key="fake")

        assert result == gemini_triage.EMPTY_RESULT
