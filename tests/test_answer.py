"""Prompt contract, CoT stripping, the LLM fallback chain and citation checks."""

from __future__ import annotations

from dataclasses import replace

import pytest
import requests

from conftest import FakeResponse
from infoforge import answer as answer_mod
from infoforge.answer import (
    LLMRequestError,
    build_prompt,
    cited_titles,
    strip_cot,
)
from infoforge.config import LLMUnreachableError
from infoforge.retrieve import Source

SOURCES = [
    Source(title="Emu War", url="http://h/content/w/Emu_War",
           text="The Emu War was a 1932 operation."),
    Source(title="Nineteen Eighty-Four", url="http://h/content/w/1984",
           text="A 1949 novel by George Orwell."),
]


def reply(content):
    return FakeResponse(200, json_data={"choices": [{"message": {"content": content}}]})


class ChainSession:
    """Replays a queued response (or raises a queued exception) per POST."""

    def __init__(self, *items):
        self.items = list(items)
        self.bodies = []
        self.urls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.urls.append(url)
        self.bodies.append(json)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# -------------------------------------------------------------------- prompt

def test_prompt_states_the_only_sources_rule():
    prompt = build_prompt("What was the Emu War?", SOURCES)
    assert "ONLY" in prompt
    assert "If the sources do not contain the answer, say so clearly." in prompt
    assert "cite the article title" in prompt
    assert "Do not invent facts" in prompt


def test_prompt_numbers_every_source_and_ends_with_the_question():
    prompt = build_prompt("What was the Emu War?", SOURCES)
    assert "Source 1: Emu War" in prompt
    assert "Source 2: Nineteen Eighty-Four" in prompt
    assert "The Emu War was a 1932 operation." in prompt
    assert prompt.strip().endswith("Question: What was the Emu War?")


# ----------------------------------------------------------------- CoT strip

def test_strip_cot_removes_think_blocks():
    assert strip_cot("<think>hmm, let me see</think>The answer is 42.") == \
        "The answer is 42."


def test_strip_cot_removes_multiline_and_unterminated_blocks():
    assert strip_cot("<think>\nstep 1\nstep 2\n</think>\nDone.") == "Done."
    assert strip_cot("Answer first.\n<think>trailing ramble") == "Answer first."


def test_strip_cot_removes_think_fences_and_stray_code_fences():
    assert strip_cot("```think\nreasoning\n```\nThe answer.") == "The answer."
    assert strip_cot("```markdown\nThe answer.\n```") == "The answer."


def test_strip_cot_leaves_a_clean_answer_alone():
    assert strip_cot("Plain answer.") == "Plain answer."


# --------------------------------------------------------------- the chain

def test_primary_answers_and_no_fallback_is_used(cfg):
    session = ChainSession(reply("According to Emu War, 1932."))
    result = answer_mod.answer_question(cfg, "q", SOURCES, session=session)
    assert result.endpoint.model_id == "test-model"
    assert len(session.urls) == 1


def test_connection_error_advances_to_the_fallback(cfg):
    session = ChainSession(
        requests.exceptions.ConnectionError("refused"),
        reply("According to Emu War, 1932."),
    )
    result = answer_mod.answer_question(cfg, "q", SOURCES, session=session)
    assert result.endpoint.model_id == "fallback-model"
    assert [a.ok for a in result.attempts] == [False, True]


def test_server_error_advances_to_the_fallback(cfg):
    session = ChainSession(FakeResponse(500, "exploded"),
                           reply("According to Emu War, 1932."))
    result = answer_mod.answer_question(cfg, "q", SOURCES, session=session)
    assert result.endpoint.model_id == "fallback-model"


def test_busy_with_zero_hold_budget_advances_to_the_fallback(cfg):
    # hold_wait_s=0 (the test fixture) restores fail-fast: a busy signal
    # advances the chain immediately instead of waiting.
    for status in (503, 429):
        session = ChainSession(FakeResponse(status, "busy"),
                               reply("According to Emu War, 1932."))
        assert answer_mod.answer_question(
            cfg, "q", SOURCES, session=session).endpoint.model_id == "fallback-model"


def test_priority_hold_waits_out_the_same_endpoint(cfg, monkeypatch):
    # StudioForge's 503 priority_hold: honour Retry-After and retry the SAME
    # endpoint — the primary must answer, no fallback involved.
    naps = []
    monkeypatch.setattr(answer_mod.time, "sleep", naps.append)
    held = replace(cfg, hold_wait_s=300)
    session = ChainSession(
        FakeResponse(503, '{"error": "priority_hold"}',
                     headers={"Retry-After": "7"}),
        reply("According to Emu War, 1932."),
    )
    result = answer_mod.answer_question(held, "q", SOURCES, session=session)
    assert result.endpoint.model_id == "test-model"
    assert len(session.urls) == 2 and session.urls[0] == session.urls[1]
    assert naps == [7.0]


def test_hold_delay_falls_back_to_json_then_default(cfg):
    assert answer_mod._hold_delay_s(
        FakeResponse(503, json_data={"retry_after_s": 42})) == 42.0
    assert answer_mod._hold_delay_s(
        FakeResponse(503, "no shape")) == answer_mod._HOLD_DEFAULT_DELAY_S
    # Clamped: never hammer (min 1s), never nap for an hour on a bad header.
    assert answer_mod._hold_delay_s(
        FakeResponse(429, headers={"Retry-After": "0"})) == 1.0
    assert answer_mod._hold_delay_s(
        FakeResponse(429, headers={"Retry-After": "3600"})) == 120.0


def test_hold_budget_exhaustion_advances_to_the_fallback(cfg, monkeypatch):
    # Simulate the clock running out after the first nap.
    clock = iter([0.0, 0.0, 100.0, 100.0])
    monkeypatch.setattr(answer_mod.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(answer_mod.time, "sleep", lambda s: None)
    held = replace(cfg, hold_wait_s=60)
    session = ChainSession(
        FakeResponse(503, "priority_hold"),
        FakeResponse(503, "priority_hold"),
        reply("According to Emu War, 1932."),
    )
    result = answer_mod.answer_question(held, "q", SOURCES, session=session)
    assert result.endpoint.model_id == "fallback-model"
    assert [a.ok for a in result.attempts] == [False, True]


def test_a_refusal_is_an_answer_not_a_fallback_trigger(cfg):
    # "not in the sources" is the grounded contract working, not a failure.
    session = ChainSession(reply("The sources do not contain that information."))
    result = answer_mod.answer_question(cfg, "q", SOURCES, session=session)
    assert result.endpoint.model_id == "test-model"
    assert len(session.urls) == 1


def test_a_bad_request_stops_the_chain(cfg):
    # A 400 means our request is wrong (bad model id / auth); it would fail the
    # same way on every endpoint, so retrying is noise.
    session = ChainSession(FakeResponse(400, "no such model"))
    with pytest.raises(LLMRequestError, match="model_id"):
        answer_mod.answer_question(cfg, "q", SOURCES, session=session)
    assert len(session.urls) == 1


def test_all_endpoints_down_raises_exit_5(cfg):
    session = ChainSession(
        requests.exceptions.ConnectionError("refused"),
        requests.exceptions.ConnectionError("refused"),
    )
    with pytest.raises(LLMUnreachableError) as exc:
        answer_mod.answer_question(cfg, "q", SOURCES, session=session)
    assert exc.value.exit_code == 5


def test_empty_content_falls_back_to_reasoning_content(cfg):
    payload = {"choices": [{"message": {"content": "",
                                        "reasoning_content": "Per Emu War: 1932."}}]}
    session = ChainSession(FakeResponse(200, json_data=payload))
    result = answer_mod.answer_question(cfg, "q", SOURCES, session=session)
    assert "1932" in result.text


def test_malformed_payload_advances_the_chain(cfg):
    session = ChainSession(FakeResponse(200, json_data={"nope": True}),
                           reply("According to Emu War, 1932."))
    result = answer_mod.answer_question(cfg, "q", SOURCES, session=session)
    assert result.endpoint.model_id == "fallback-model"


class HeaderSession(ChainSession):
    """ChainSession that also records the request headers."""

    def __init__(self, *items):
        super().__init__(*items)
        self.headers = {}

    def post(self, url, headers=None, json=None, timeout=None):
        self.headers = dict(headers or {})
        return super().post(url, headers=headers, json=json, timeout=timeout)


def test_authorization_header_is_sent_when_a_key_is_set(cfg):
    keyed = replace(cfg, api_key="secret-key")
    session = HeaderSession(reply("hi"))
    answer_mod.call_endpoint(keyed, keyed.primary, "prompt", session=session)
    assert session.headers["Authorization"] == "Bearer secret-key"


def test_no_authorization_header_without_a_key(cfg):
    session = HeaderSession(reply("hi"))
    answer_mod.call_endpoint(cfg, cfg.primary, "prompt", session=session)
    assert "Authorization" not in session.headers


# ------------------------------------------------------------- studioforge

def test_priority_field_is_absent_by_default(cfg):
    session = ChainSession(reply("ok"))
    answer_mod.answer_question(cfg, "q", SOURCES, session=session)
    assert "priority" not in session.bodies[0]


def test_priority_3_only_when_studioforge_is_true(cfg):
    sf = replace(cfg, studioforge=True)
    session = ChainSession(reply("ok"))
    answer_mod.answer_question(sf, "q", SOURCES, session=session)
    assert session.bodies[0]["priority"] == 3


def test_body_carries_the_endpoint_model_not_the_primary(cfg):
    session = ChainSession(requests.exceptions.ConnectionError("x"), reply("ok"))
    answer_mod.answer_question(cfg, "q", SOURCES, session=session)
    assert session.bodies[1]["model"] == "fallback-model"


# -------------------------------------------------------------- citations

def test_cited_titles_finds_a_mentioned_title():
    assert cited_titles("According to Emu War, it was 1932.", SOURCES) == ["Emu War"]


def test_cited_titles_ignores_punctuation_differences():
    assert cited_titles("See Nineteen Eighty Four for details.", SOURCES) == \
        ["Nineteen Eighty-Four"]


def test_cited_titles_is_empty_for_an_ungrounded_answer():
    assert cited_titles("It was in 1932, I think.", SOURCES) == []


def test_answer_flags_an_uncited_reply(cfg):
    session = ChainSession(reply("It was 1932, I think."))
    result = answer_mod.answer_question(cfg, "q", SOURCES, session=session)
    assert result.uncited is True
    assert result.cited == []


def test_answer_does_not_flag_a_cited_reply(cfg):
    session = ChainSession(reply("According to Emu War, 1932."))
    result = answer_mod.answer_question(cfg, "q", SOURCES, session=session)
    assert result.uncited is False


def test_attempts_never_carry_the_api_key(cfg):
    keyed = replace(cfg, api_key="secret-key")
    session = ChainSession(reply("According to Emu War, 1932."))
    result = answer_mod.answer_question(keyed, "q", SOURCES, session=session)
    assert "secret-key" not in repr(result.attempts)
