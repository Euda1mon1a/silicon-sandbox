"""Tests for the domain allowlist HTTP proxy."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "sandbox-engine"))

from sandbox_engine.proxy import AllowlistProxy


class TestDomainMatching:
    """Test domain allowlist matching logic."""

    def setup_method(self):
        self.proxy = AllowlistProxy(
            allowed_domains=[
                "pypi.org",
                "*.pypi.org",
                "files.pythonhosted.org",
                "github.com",
                "*.github.com",
                "api.anthropic.com",
            ],
            deny_all=True,
        )

    def test_exact_match(self):
        assert self.proxy.is_domain_allowed("pypi.org") is True
        assert self.proxy.is_domain_allowed("github.com") is True
        assert self.proxy.is_domain_allowed("api.anthropic.com") is True

    def test_wildcard_subdomain(self):
        assert self.proxy.is_domain_allowed("upload.pypi.org") is True
        assert self.proxy.is_domain_allowed("raw.github.com") is True  # matches *.github.com
        assert self.proxy.is_domain_allowed("api.github.com") is True
        assert self.proxy.is_domain_allowed("evil.github.com") is True  # still matches wildcard

    def test_blocked_domains(self):
        assert self.proxy.is_domain_allowed("evil.com") is False
        assert self.proxy.is_domain_allowed("google.com") is False
        assert self.proxy.is_domain_allowed("malware.example.org") is False

    def test_case_insensitive(self):
        assert self.proxy.is_domain_allowed("PYPI.ORG") is True
        assert self.proxy.is_domain_allowed("GitHub.Com") is True

    def test_strips_port(self):
        assert self.proxy.is_domain_allowed("pypi.org:443") is True
        assert self.proxy.is_domain_allowed("evil.com:80") is False

    def test_deny_all_false_allows_everything(self):
        open_proxy = AllowlistProxy(allowed_domains=[], deny_all=False)
        assert open_proxy.is_domain_allowed("anything.com") is True

    def test_wildcard_matches_base_domain(self):
        """*.example.com should also match example.com itself (via base check)."""
        proxy = AllowlistProxy(
            allowed_domains=["*.example.com"],
            deny_all=True,
        )
        assert proxy.is_domain_allowed("example.com") is True
        assert proxy.is_domain_allowed("sub.example.com") is True
        assert proxy.is_domain_allowed("deep.sub.example.com") is True
        assert proxy.is_domain_allowed("notexample.com") is False


class TestProxyLifecycle:
    """Test proxy server start/stop."""

    def test_start_and_stop(self):
        proxy = AllowlistProxy(port=19876, allowed_domains=["example.com"])
        proxy.start()
        assert proxy._server is not None
        assert proxy._thread is not None
        assert proxy._thread.is_alive()
        proxy.stop()
        assert proxy._server is None

    def test_url_property(self):
        proxy = AllowlistProxy(host="127.0.0.1", port=8098)
        assert proxy.url == "http://127.0.0.1:8098"
