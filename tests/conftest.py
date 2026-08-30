"""Shared fixtures.  No network, no podman, no filesystem outside tmp_path."""

from __future__ import annotations

import pytest

from infoforge.config import Config, Endpoint, Zim


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Keep every test off the real config and the real event log."""
    monkeypatch.delenv("INFOFORGE_CONFIG", raising=False)
    monkeypatch.setenv("INFOFORGE_LOG", str(tmp_path / "log.jsonl"))
    return tmp_path


@pytest.fixture
def cfg(tmp_path) -> Config:
    zim_dir = tmp_path / "zims"
    zim_dir.mkdir()
    (zim_dir / "wikipedia_en_all_nopic.zim").write_bytes(b"not a real zim")
    return Config(
        model_id="test-model",
        base_url="http://127.0.0.1:1234/v1",
        api_key="",
        fallbacks=(Endpoint("fallback-model", "http://127.0.0.1:1235/v1"),),
        kiwix_url="http://127.0.0.1:8080",
        zim_dir=zim_dir,
        zims=(Zim(file="wikipedia_en_all_nopic.zim", name="English Wikipedia"),),
        max_articles=3,
        max_chars=500,
        search_pool=15,
        timeout_s=5,
    )


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


@pytest.fixture
def fake_response():
    return FakeResponse


def search_xml(*items: tuple[str, str, str]) -> str:
    """Build a kiwix-serve RSS 2.0 search response from (title, path, snippet)."""
    entries = "\n".join(
        f"<item><title>{t}</title><link>{p}</link>"
        f"<description>{s}</description></item>"
        for t, p, s in items
    )
    return f"<?xml version='1.0'?><rss version='2.0'><channel>{entries}</channel></rss>"
