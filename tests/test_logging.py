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
    assert set(fields) == {"question_hmac", "question_chars"}
    assert fields["question_chars"] == len("Who built the Eiffel Tower?")
    assert len(fields["question_hmac"]) == 16
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


# ----------------------------------- review fix (opus): keyed question digest

def test_question_digest_is_keyed_not_a_plain_sha256(tmp_path, monkeypatch):
    # An unkeyed SHA-256 of a short question lets anyone holding the log
    # confirm a guessed question by hashing it.
    import hashlib
    monkeypatch.setenv("INFOFORGE_LOG", str(tmp_path / "a" / "log.jsonl"))
    question = "Who built the Eiffel Tower?"
    digest = logging_.question_fields(question)["question_hmac"]
    assert digest != hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]
    key_file = tmp_path / "a" / logging_.KEY_NAME
    assert len(key_file.read_bytes()) == 32
    # Another install (another key) produces another digest for the same text.
    monkeypatch.setenv("INFOFORGE_LOG", str(tmp_path / "b" / "log.jsonl"))
    assert logging_.question_fields(question)["question_hmac"] != digest


def test_an_existing_key_is_reused_across_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("INFOFORGE_LOG", str(tmp_path / "log.jsonl"))
    first = logging_.question_fields("q")
    logging_._KEYS.clear()  # a fresh process: nothing cached
    assert logging_.question_fields("q") == first


def test_an_unusable_key_drops_the_digest_but_keeps_the_length(tmp_path, monkeypatch):
    monkeypatch.setenv("INFOFORGE_LOG", str(tmp_path / "log.jsonl"))
    (tmp_path / logging_.KEY_NAME).write_bytes(b"short")
    assert logging_.question_fields("hello") == {"question_chars": 5}


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_the_question_key_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("INFOFORGE_LOG", str(tmp_path / "log.jsonl"))
    logging_.question_fields("q")
    assert (tmp_path / logging_.KEY_NAME).stat().st_mode & 0o777 == 0o600


# ------------------------------------------------- rotation lock (#12)

@pytest.fixture(autouse=True)
def _reset_scrub_flag():
    logging_._scrub_done = False
    yield
    logging_._scrub_done = False


def _big(path):
    path.write_text("x" * (logging_.MAX_BYTES + 10) + "\n", encoding="utf-8")


def test_rotation_removes_its_lock_file(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv("INFOFORGE_LOG", str(path))
    _big(path)
    logging_.event("doctor", ok=True)
    assert path.with_name("log.jsonl.1").exists()
    assert not path.with_name("log.jsonl.lock").exists()
    assert read(path)[-1]["event"] == "doctor"


def test_fresh_lock_skips_rotation_but_still_appends(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv("INFOFORGE_LOG", str(path))
    _big(path)
    lock = path.with_name("log.jsonl.lock")
    lock.write_text("")
    logging_.event("doctor", ok=True)
    assert not path.with_name("log.jsonl.1").exists()
    assert lock.exists()  # not ours, left alone
    assert path.read_text(encoding="utf-8").splitlines()[-1].startswith('{"ts"')


def test_stale_lock_is_cleared_and_rotation_proceeds(tmp_path, monkeypatch):
    import time
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv("INFOFORGE_LOG", str(path))
    _big(path)
    lock = path.with_name("log.jsonl.lock")
    lock.write_text("")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    logging_.event("doctor", ok=True)
    assert path.with_name("log.jsonl.1").exists()
    assert not lock.exists()


# ------------------------------------- plaintext scrub of 2.0 logs (#18)

OLD = [
    {"ts": 1.0, "event": "retrieve", "question": "secret topic", "sources": 2,
     "titles": ["Alpha", "Beta"]},
    {"ts": 2.0, "event": "ask", "question": "another", "model_id": "m",
     "cited": ["Alpha"], "uncited": False},
    {"ts": 3.0, "event": "search", "question_sha256": "ab" * 32, "sources": 1},
    {"ts": 4.0, "event": "doctor", "ok": True},
]


def _old_log(path, extra=()):
    lines = [json.dumps(r, ensure_ascii=False) for r in OLD] + list(extra)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_scrub_rewrites_2_0_log_and_rotated_siblings(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv("INFOFORGE_LOG", str(path))
    _old_log(path)
    _old_log(path.with_name("log.jsonl.2"))
    logging_.question_fields("anything")
    for f in (path, path.with_name("log.jsonl.2")):
        text = f.read_text(encoding="utf-8")
        for needle in ("secret topic", "another", "Alpha", "question_sha256", '"titles"'):
            assert needle not in text
        assert (f.stat().st_mode & 0o777) == 0o600
    recs = read(path)
    assert recs[0]["question_chars"] == len("secret topic")
    assert recs[1]["question_chars"] == len("another")
    assert recs[3] == OLD[3]
    assert recs[-1]["event"] == "log_scrub" and recs[-1]["files"] == 2
    assert recs[-1]["records"] == 6
    assert not path.with_name("log.jsonl.lock").exists()


def test_scrub_leaves_clean_records_byte_identical(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv("INFOFORGE_LOG", str(path))
    _old_log(path)
    before = path.read_text(encoding="utf-8").splitlines()
    logging_.question_fields("anything")
    after = path.read_text(encoding="utf-8").splitlines()
    assert after[3] == before[3]


def test_scrub_runs_once_per_process(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv("INFOFORGE_LOG", str(path))
    _old_log(path)
    logging_.question_fields("a")
    _old_log(path)  # plaintext reappears (e.g. an old binary): not rescrubbed
    logging_.question_fields("b")
    assert "secret topic" in path.read_text(encoding="utf-8")


def test_scrub_skipped_when_log_questions_is_on(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv("INFOFORGE_LOG", str(path))
    _old_log(path)
    before = path.read_bytes()
    assert logging_.question_fields("q", keep=True) == {"question": "q"}
    assert path.read_bytes() == before


def test_scrub_preserves_unparsable_lines(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv("INFOFORGE_LOG", str(path))
    junk = '{"question": "broken json'
    _old_log(path, extra=[junk])
    logging_.question_fields("x")
    assert junk in path.read_text(encoding="utf-8").splitlines()


def test_scrub_with_no_log_files_is_a_noop(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv("INFOFORGE_LOG", str(path))
    logging_.question_fields("x")
    assert not path.exists()


def test_scrub_retries_when_the_lock_is_held(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv("INFOFORGE_LOG", str(path))
    _old_log(path)
    lock = path.with_name("log.jsonl.lock")
    lock.write_text("")
    logging_.question_fields("x")
    assert "secret topic" in path.read_text(encoding="utf-8")
    lock.unlink()
    logging_.question_fields("x")
    assert "secret topic" not in path.read_text(encoding="utf-8")
