"""The jsonl event log: append-only, secret-free, never fatal."""

from __future__ import annotations

import json

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
