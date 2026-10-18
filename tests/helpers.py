"""Shared stubs for the CLI-level tests."""

from __future__ import annotations

from infoforge import answer as answer_mod
from infoforge import retrieve
from infoforge.answer import Answer
from infoforge.config import Endpoint
from infoforge.retrieve import Source

SOURCES = [Source(title="Emu War", url="http://127.0.0.1:18080/content/w/Emu_War",
                  text="1932 operation.", score=12.0)]


def stub_answer(monkeypatch, text="According to Emu War, 1932.", uncited=False,
                verdict="answered"):
    def fake(cfg, question, sources, session=None):
        return Answer(text=text, endpoint=Endpoint("test-model", "http://x/v1"),
                      cited=[] if uncited else ["Emu War"], uncited=uncited,
                      verdict=verdict, verdict_source="model")
    monkeypatch.setattr(answer_mod, "answer_question", fake)


def stub_retrieve(monkeypatch, sources=SOURCES):
    monkeypatch.setattr(
        retrieve, "retrieve",
        lambda cfg, q, max_articles=None, session=None: list(sources),
    )
