"""The DisPatch add-on: metadata, registration, and the wiki_search tool."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tomllib
from pathlib import Path

import pytest
from helpers import SOURCES

import infoforge_dispatch as ext
from infoforge import kiwix, retrieve
from infoforge.retrieve import Source

ROOT = Path(__file__).resolve().parent.parent


class FakeCtx:
    """The slice of DisPatch's ExtensionContext the add-on uses."""

    def __init__(self):
        self.registered: list = []
        self.sealed = False

    def register_live_tools(self, specs):
        if self.sealed:
            raise RuntimeError("register_live_tools is only allowed inside setup(ctx)")
        self.registered.extend(specs)


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------- the handshake metadata

def test_extension_metadata_satisfies_dispatchs_handshake_rules():
    meta = ext.EXTENSION
    # Mirrors DisPatch's backend/app/extensions.py::_meta validation.
    assert re.fullmatch(r"[a-z][a-z0-9-]{1,31}", meta["id"])
    assert meta["api"] == [1, 1]
    assert len(meta["name"]) <= 80 and len(meta["version"]) <= 40
    assert re.fullmatch(r"[A-Za-z_][\w.]*:[A-Za-z_][\w.]*", meta["setup"])
    assert all(isinstance(n, str) for n in meta["env"])


def test_version_tracks_the_package():
    from infoforge import __version__
    assert ext.EXTENSION["version"] == __version__


def test_setup_target_resolves_to_the_setup_function():
    module, _, attr = ext.EXTENSION["setup"].partition(":")
    assert module == "infoforge_dispatch" and getattr(ext, attr) is ext.setup


def test_entry_point_is_declared_in_pyproject():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    eps = data["project"]["entry-points"]["dispatch.extensions"]
    assert eps == {"infoforge": "infoforge_dispatch:EXTENSION"}
    assert "dispatch" in data["project"]["optional-dependencies"]
    assert "infoforge_dispatch" in json.dumps(data["tool"]["hatch"])


def test_importing_without_dispatch_installed_is_safe():
    for name in list(sys.modules):
        if name == "infoforge_dispatch":
            del sys.modules[name]
    assert "backend" not in sys.modules and "app" not in sys.modules
    import importlib
    mod = importlib.import_module("infoforge_dispatch")
    assert mod.EXTENSION["id"] == "infoforge"


# --------------------------------------------------------------- registration

def test_setup_registers_one_valid_read_only_tool():
    ctx = FakeCtx()
    ext.setup(ctx)
    assert [s["name"] for s in ctx.registered] == ["wiki_search"]
    spec = ctx.registered[0]
    # DisPatch livetools.validate_live_tools rules:
    assert re.fullmatch(r"[a-z][a-z0-9_]{1,30}", spec["name"])
    assert 1 <= len(spec["description"]) <= 1000
    assert spec["parameters"]["type"] == "object"
    json.dumps(spec["parameters"])
    assert spec["parameters"]["required"] == ["query"]
    assert asyncio.iscoroutinefunction(spec["fn"])
    assert spec["summary"]({"query": "emu war"}) == "Offline Wikipedia: emu war"
    assert spec["summary"]({}).startswith("Offline Wikipedia")


def test_registering_after_setup_returned_is_the_hosts_error():
    ctx = FakeCtx()
    ext.setup(ctx)
    ctx.sealed = True
    with pytest.raises(RuntimeError):
        ext.setup(ctx)


# ------------------------------------------------------------- the tool itself

def test_wiki_search_returns_cited_article_text(config_file):
    seen = {}

    def fake(cfg, q, max_articles=None, session=None):
        seen.update(q=q, n=max_articles)
        return list(SOURCES)
    retrieve_orig = retrieve.retrieve
    retrieve.retrieve = fake
    try:
        out = run(ext.wiki_search({"query": "  Emu   War ", "max_articles": 2}))
    finally:
        retrieve.retrieve = retrieve_orig
    assert seen == {"q": "Emu War", "n": 2}
    assert out.startswith("[1] Emu War (http://127.0.0.1:18080/content/w/Emu_War)")
    assert "1932 operation." in out
    assert "Attribution:" in out


def test_wiki_search_never_starts_the_container(config_file, monkeypatch):
    monkeypatch.setattr(kiwix, "is_up", lambda cfg, session=None, timeout=5: False)
    monkeypatch.setattr(kiwix, "ensure", lambda cfg: pytest.fail("must not create"))
    out = run(ext.wiki_search({"query": "emu"}))
    assert out.startswith("Offline Wikipedia is unavailable (kiwix_unreachable).")
    assert "infoforge serve start" in out
    assert "config.yaml" not in out and str(config_file) not in out  # no paths in results


def test_wiki_search_reports_an_empty_library_plainly(config_file, monkeypatch):
    monkeypatch.setattr(retrieve, "retrieve", lambda *a, **k: [])
    out = run(ext.wiki_search({"query": "zqxv"}))
    assert out == "No articles found for 'zqxv' in the offline library."


def test_wiki_search_without_a_config_says_so_without_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("INFOFORGE_CONFIG", str(tmp_path / "absent.yaml"))
    out = run(ext.wiki_search({"query": "emu"}))
    assert "config_missing" in out and str(tmp_path) not in out


@pytest.mark.parametrize("args", [{}, {"query": ""}, {"query": "   "}, {"query": None}, None])
def test_wiki_search_rejects_an_empty_query(args):
    assert "query must be a non-empty string" in run(ext.wiki_search(args))


@pytest.mark.parametrize("raw,expected", [
    (None, 3), ("5", 3), (True, 3), (0, 1), (-4, 1), (2, 2), (99, 5), (3.5, 3)])
def test_article_count_is_clamped(raw, expected):
    assert ext._clean_count(raw) == expected


def test_a_dash_query_is_just_text_not_an_option(config_file, monkeypatch):
    seen = {}

    def fake(cfg, q, max_articles=None, session=None):
        seen["q"] = q
        return list(SOURCES)
    monkeypatch.setattr(retrieve, "retrieve", fake)
    run(ext.wiki_search({"query": "--version"}))
    assert seen["q"] == "--version"


def test_long_queries_are_bounded():
    assert len(ext._clean_query("x" * 5000)) == ext.MAX_QUERY_CHARS


def test_results_stay_under_the_budget(config_file, monkeypatch):
    big = [Source(title=f"T{i}", url=f"http://127.0.0.1:18080/content/w/T{i}",
                  text="word " * 2000, score=1.0) for i in range(5)]
    monkeypatch.setattr(retrieve, "retrieve", lambda *a, **k: big)
    out = run(ext.wiki_search({"query": "x", "max_articles": 5}))
    body = out.split("\n\nAttribution:")[0]
    assert len(body) <= ext.RESULT_CHARS + 5 * 100
    assert out.count(" ...") == 5


def test_a_slow_lookup_times_out_politely(monkeypatch):
    import threading
    release = threading.Event()

    def slow(query, n):
        release.wait(0.3)
        return "late"
    monkeypatch.setattr(ext, "_lookup", slow)
    monkeypatch.setattr(ext, "TIMEOUT_S", 0.05)
    try:
        out = run(ext.wiki_search({"query": "x"}))
    finally:
        release.set()
    assert "timed out" in out
