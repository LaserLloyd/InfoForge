"""zimify: prepare a folder, and degrade usefully when zimwriterfs is absent."""

from __future__ import annotations

import pytest

from infoforge import zimify as zimify_mod
from infoforge.zimify import ZimifyError, zimify


@pytest.fixture
def docs(tmp_path):
    src = tmp_path / "docs"
    src.mkdir()
    (src / "notes.md").write_text("# Notes\n\nhello\n", encoding="utf-8")
    (src / "page.html").write_text("<p>page</p>", encoding="utf-8")
    return src


def test_missing_input_dir_is_a_pointed_error(tmp_path):
    with pytest.raises(ZimifyError, match="input directory not found"):
        zimify(tmp_path / "nope", tmp_path / "out.zim")


def test_empty_input_dir_is_refused(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ZimifyError, match="no files"):
        zimify(tmp_path / "empty", tmp_path / "out.zim")


def test_prepares_a_site_and_reports_the_command_without_zimwriterfs(
        docs, tmp_path, monkeypatch):
    monkeypatch.setattr(zimify_mod, "_have", lambda tool: False)
    report = zimify(docs, tmp_path / "out.zim", title="Docs")
    assert report["built"] is False
    assert report["files"] == 2
    prep = report["prep_dir"]
    assert (prep / "index.html").is_file()
    assert (prep / "page.html").is_file()
    assert (prep / "notes.md").is_file()  # no pandoc -> copied through
    assert report["command"][0] == "zimwriterfs"
    assert "--title=Docs" in report["command"]


def test_index_links_and_escapes_every_file(docs, tmp_path, monkeypatch):
    monkeypatch.setattr(zimify_mod, "_have", lambda tool: False)
    (docs / "a&b.html").write_text("<p>x</p>", encoding="utf-8")
    report = zimify(docs, tmp_path / "out.zim")
    index = (report["prep_dir"] / "index.html").read_text(encoding="utf-8")
    assert 'href="page.html"' in index
    assert "a&amp;b.html" in index


def test_build_runs_zimwriterfs_with_fixed_argv(docs, tmp_path, monkeypatch):
    calls = {}

    def fake_run(argv, cwd=None, capture_output=False, text=False, check=False):
        import subprocess
        calls["argv"] = argv
        calls["cwd"] = cwd
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(zimify_mod, "_have", lambda tool: tool == "zimwriterfs")
    monkeypatch.setattr(zimify_mod.subprocess, "run", fake_run)
    report = zimify(docs, tmp_path / "out.zim", title="T", description="D")
    assert report["built"] is True
    assert isinstance(calls["argv"], list)  # argv list, never a shell string
    assert calls["argv"][0] == "zimwriterfs"
    assert "--title=T" in calls["argv"]
    assert calls["cwd"] == report["prep_dir"]


def test_a_failing_build_is_reported(docs, tmp_path, monkeypatch):
    def fake_run(argv, cwd=None, capture_output=False, text=False, check=False):
        import subprocess
        return subprocess.CompletedProcess(argv, 1, "", "disk full")

    monkeypatch.setattr(zimify_mod, "_have", lambda tool: tool == "zimwriterfs")
    monkeypatch.setattr(zimify_mod.subprocess, "run", fake_run)
    with pytest.raises(ZimifyError, match="disk full"):
        zimify(docs, tmp_path / "out.zim")


def test_prep_dir_is_rebuilt_from_scratch(docs, tmp_path, monkeypatch):
    monkeypatch.setattr(zimify_mod, "_have", lambda tool: False)
    first = zimify(docs, tmp_path / "out.zim")
    stale = first["prep_dir"] / "stale.html"
    stale.write_text("old", encoding="utf-8")
    second = zimify(docs, tmp_path / "out.zim")
    assert not (second["prep_dir"] / "stale.html").exists()
