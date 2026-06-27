"""
Tests for the SSRF guard used by /test-clip before it fetches a caller-supplied
image URL server-side. Lives in utils.py (pure stdlib) so it runs without the
torch/flask/google stack. IP literals are used so getaddrinfo resolves offline.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ebayscout.utils import is_safe_fetch_url


class TestIsSafeFetchUrl:
    def test_rejects_empty(self):
        assert is_safe_fetch_url(None) is False
        assert is_safe_fetch_url("") is False

    def test_rejects_non_http_schemes(self):
        assert is_safe_fetch_url("file:///etc/passwd") is False
        assert is_safe_fetch_url("ftp://1.1.1.1/x") is False
        assert is_safe_fetch_url("gopher://1.1.1.1/x") is False

    def test_rejects_missing_host(self):
        assert is_safe_fetch_url("http:///just/a/path") is False

    def test_rejects_cloud_metadata_server(self):
        assert is_safe_fetch_url("http://169.254.169.254/computeMetadata/v1/") is False

    def test_rejects_loopback(self):
        assert is_safe_fetch_url("http://127.0.0.1/x") is False
        assert is_safe_fetch_url("http://[::1]:8080/x") is False

    def test_rejects_private_ranges(self):
        assert is_safe_fetch_url("http://10.0.0.5/x") is False
        assert is_safe_fetch_url("http://192.168.1.1/x") is False
        assert is_safe_fetch_url("http://172.16.0.1/x") is False

    def test_accepts_public_ip_literals(self):
        assert is_safe_fetch_url("https://93.184.216.34/img.png") is True
        assert is_safe_fetch_url("http://1.1.1.1/img.png") is True
