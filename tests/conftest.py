"""Shared fixtures.  No network, no podman, no filesystem outside tmp_path.

That promise is enforced, not just intended: the autouse ``no_real_network``
fixture makes any attempt to open an outbound socket, or to spawn podman /
pandoc / zimwriterfs, fail the test.  Fixture URLs use 11234 / 11235 / 18080
(never the 1234 / 1235 / 8080 a co-located StudioForge listens on), so even a
guard bug could not reach a live service.
"""

from __future__ import annotations

import os
import socket
import subprocess

import pytest

from infoforge import kiwix
from infoforge.config import Config, Endpoint, Zim

#: External programs InfoForge shells out to; a test must never really run them.
FORBIDDEN_PROGRAMS = {"podman", "pandoc", "zimwriterfs", "markdown", "kiwix-serve"}


class RealNetworkAccess(AssertionError):
    """A test tried to touch the real network or a real external program."""


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Fail any test that opens a real socket or runs a real external tool.

    asyncio (used by the MCP tests) builds its self-pipe from a loopback
    ``socketpair``, which on Windows connects to a port the *same process* is
    listening on; that is allowed, anything else is not.
    """
    listening: set[int] = set()
    real_listen = socket.socket.listen

    def listen(self, *args):
        result = real_listen(self, *args)
        try:
            listening.add(self.getsockname()[1])
        except OSError:
            pass
        return result

    def guard(address):
        if isinstance(address, tuple) and len(address) >= 2 and address[1] in listening:
            return
        raise RealNetworkAccess(
            f"test tried to connect a real socket to {address!r}; patch the HTTP call "
            "(see FakeSession in tests/test_kiwix.py)"
        )

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def connect(self, address):
        guard(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        guard(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "listen", listen)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)

    real_popen_init = subprocess.Popen.__init__

    def popen_init(self, args, *a, **k):
        first = args if isinstance(args, str) else (args[0] if args else "")
        name = os.path.basename(str(first)).lower().removesuffix(".exe")
        if name in FORBIDDEN_PROGRAMS:
            raise RealNetworkAccess(f"test tried to run the real program {name!r}")
        return real_popen_init(self, args, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", popen_init)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Keep every test off the real config and the real event log."""
    monkeypatch.delenv("INFOFORGE_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for name in list(os.environ):
        if name.startswith("INFOFORGE_") and name != "INFOFORGE_LOG":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("INFOFORGE_LOG", str(tmp_path / "log.jsonl"))
    return tmp_path


@pytest.fixture
def cfg(tmp_path) -> Config:
    zim_dir = tmp_path / "zims"
    zim_dir.mkdir()
    (zim_dir / "wikipedia_en_all_nopic.zim").write_bytes(b"not a real zim")
    return Config(
        model_id="test-model",
        base_url="http://127.0.0.1:11234/v1",
        api_key="",
        fallbacks=(Endpoint("fallback-model", "http://127.0.0.1:11235/v1"),),
        kiwix_url="http://127.0.0.1:18080",
        kiwix_port=18080,
        zim_dir=zim_dir,
        zims=(Zim(file="wikipedia_en_all_nopic.zim", name="English Wikipedia"),),
        max_articles=3,
        max_chars=500,
        search_pool=15,
        timeout_s=5,
        hold_wait_s=0,
    )


CONFIG_TEXT = """
model_id: test-model
base_url: http://127.0.0.1:11234/v1
zim_dir: {zim_dir}
zims:
  - file: test.zim
    enabled: true
"""


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """A valid config file (INFOFORGE_CONFIG) whose kiwix always answers."""
    zim_dir = tmp_path / "zims"
    zim_dir.mkdir()
    (zim_dir / "test.zim").write_bytes(b"x")
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_TEXT.format(zim_dir=zim_dir), encoding="utf-8")
    monkeypatch.setenv("INFOFORGE_CONFIG", str(path))
    # Never touch podman or the network from a CLI test.
    monkeypatch.setattr(kiwix, "is_up", lambda cfg, session=None, timeout=5: True)
    monkeypatch.setattr(kiwix, "podman_available", lambda: True)
    return path


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code=200, text="", json_data=None, headers=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data
        self.headers = headers or {}

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
