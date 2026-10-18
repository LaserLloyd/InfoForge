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
    # zim-tools requires these; a printed command without them cannot be run.
    assert "--creator=InfoForge" in report["command"]
    assert "--publisher=InfoForge" in report["command"]
    # zim-tools 3.x also refuses ("too few arguments") without an illustration
    # and a name (seen with zimwriterfs 3.8.0 in the 2026-10 review).
    assert "--illustration=illustration.png" in report["command"]
    assert "--name=out" in report["command"]
    assert report["command"][-2:] == [".", str(tmp_path.resolve() / "out.zim")]


def test_a_48x48_png_illustration_is_written_into_the_prepared_dir(
        docs, tmp_path, monkeypatch):
    import struct

    monkeypatch.setattr(zimify_mod, "_have", lambda tool: False)
    report = zimify(docs, tmp_path / "out.zim")
    png = (report["prep_dir"] / "illustration.png").read_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", png[16:24])      # IHDR starts at byte 16
    assert (width, height) == (48, 48)
    # index.html links the inputs only, not the generated illustration
    index = (report["prep_dir"] / "index.html").read_text(encoding="utf-8")
    assert "illustration.png" not in index


def test_an_illustration_supplied_by_the_input_folder_is_kept(docs, tmp_path, monkeypatch):
    monkeypatch.setattr(zimify_mod, "_have", lambda tool: False)
    (docs / "illustration.png").write_bytes(b"mine")
    report = zimify(docs, tmp_path / "out.zim")
    assert (report["prep_dir"] / "illustration.png").read_bytes() == b"mine"


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
    # zim-tools 3.2 refuses a build whose LongDescription is shorter than the Description
    assert "--description=D" in calls["argv"] and "--longDescription=D" in calls["argv"]
    assert "--creator=InfoForge" in calls["argv"]
    assert "--publisher=InfoForge" in calls["argv"]
    assert calls["cwd"] == report["prep_dir"]


def test_creator_and_publisher_are_overridable(docs, tmp_path, monkeypatch):
    monkeypatch.setattr(zimify_mod, "_have", lambda tool: False)
    report = zimify(docs, tmp_path / "out.zim", creator="Ada", publisher="Press")
    assert "--creator=Ada" in report["command"]
    assert "--publisher=Press" in report["command"]


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


def test_zimify_dot_takes_the_folder_name_as_title(docs, tmp_path, monkeypatch):
    # review fix (opus): `infoforge zimify .` gave --title= (empty), which
    # zimwriterfs refuses: Title is mandatory metadata.
    monkeypatch.setattr(zimify_mod, "_have", lambda tool: False)
    monkeypatch.chdir(docs)
    from pathlib import Path
    report = zimify(Path("."), tmp_path / "out.zim")
    assert "--title=docs" in report["command"]
    assert "<title>docs</title>" in (report["prep_dir"] / "index.html").read_text(
        encoding="utf-8")
