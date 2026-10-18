"""The jsonl event log: append-only, secret-free, never fatal."""

from __future__ import annotations

import json
import os

import pytest

from infoforge import logging_


def read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_event_appends_one_json_object_per_line(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv("INFOFORGE_LOG", str(path))
    logging_.event("ask", question="q", sources=2)
    logging_.event("doctor", ok=True)
    records = read(path)
    assert [r["event"] for r in records] == ["ask", "doctor"]
    assert records[0]["sources"] == 2
    assert "ts" in records[0]


def test_secret_shaped_fields_are_dropped(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv("INFOFORGE_LOG", str(path))
    logging_.event("llm_attempt", model_id="m", api_key="sk-live",
                   Authorization="Bearer x", token="t", ok=True)
    record = read(path)[0]
    assert record["model_id"] == "m"
    assert "sk-live" not in json.dumps(record)
    assert set(record) == {"ts", "event", "model_id", "ok"}


def test_logging_creates_the_directory(tmp_path, monkeypatch):
    path = tmp_path / "deep" / "state" / "log.jsonl"
    monkeypatch.setenv("INFOFORGE_LOG", str(path))
    logging_.event("ask")
    assert path.is_file()


def test_logging_never_raises(tmp_path, monkeypatch):
    # An unwritable log must not take an answer down with it.
    monkeypatch.setenv("INFOFORGE_LOG", str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(logging_.json, "dumps", lambda *a, **k: 1 / 0)
    logging_.event("ask")  # must not raise


# ------------------------------------------------------------ privacy (2.1)

def test_questions_are_hashed_by_default():
    fields = logging_.question_fields("Who built the Eiffel Tower?")
    assert set(fields) == {"question_sha256", "question_chars"}
    assert fields["question_chars"] == len("Who built the Eiffel Tower?")
    assert len(fields["question_sha256"]) == 16
    assert "Eiffel" not in json.dumps(fields)
    # Stable: the same question hashes the same, so repeats are visible.
    assert logging_.question_fields("Who built the Eiffel Tower?") == fields


def test_questions_are_stored_only_when_opted_in():
    assert logging_.question_fields("hello", keep=True) == {"question": "hello"}


def test_log_rotates_at_the_size_limit_and_keeps_a_few_files(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv("INFOFORGE_LOG", str(path))
    monkeypatch.setattr(logging_, "MAX_BYTES", 200)
    monkeypatch.setattr(logging_, "KEEP", 2)
    for i in range(40):
        logging_.event("ask", n=i, pad="x" * 40)
    names = sorted(p.name for p in tmp_path.glob("log.jsonl*"))
    assert names == ["log.jsonl", "log.jsonl.1", "log.jsonl.2"]
    assert not (tmp_path / "log.jsonl.3").exists()
    newest = read(path)
    assert newest and newest[-1]["n"] == 39


def test_rotation_ignores_a_missing_file(tmp_path):
    logging_._rotate(tmp_path / "absent.jsonl")  # must not raise


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_log_file_is_created_0600_and_tightened_if_loose(tmp_path, monkeypatch):
    path = tmp_path / "state" / "log.jsonl"
    monkeypatch.setenv("INFOFORGE_LOG", str(path))
    logging_.event("ask")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    path.chmod(0o644)
    logging_.event("ask")
    assert path.stat().st_mode & 0o777 == 0o600
