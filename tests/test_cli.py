"""CLI wiring and the exit-code contract.

Retrieval and answering are stubbed: what is under test here is which code the
process returns and what it prints, not the pipeline (covered elsewhere).
"""

from __future__ import annotations

import json

import pytest

from infoforge import answer as answer_mod
from infoforge import cli, kiwix, retrieve
from infoforge.answer import Answer
from infoforge.config import (
    EXIT_CITATION,
    EXIT_CONFIG,
    EXIT_LLM,
    EXIT_NO_GROUNDING,
    EXIT_NOT_IN_SOURCES,
    EXIT_OK,
    Endpoint,
    LLMUnreachableError,
)
from infoforge.retrieve import Source

CONFIG_TEXT = """
model_id: test-model
base_url: http://127.0.0.1:1234/v1
zim_dir: {zim_dir}
zims:
  - file: test.zim
    enabled: true
"""

SOURCES = [Source(title="Emu War", url="http://127.0.0.1:8080/content/w/Emu_War",
                  text="1932 operation.", score=12.0)]


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    zim_dir = tmp_path / "zims"
    zim_dir.mkdir()
    (zim_dir / "test.zim").write_bytes(b"x")
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_TEXT.format(zim_dir=zim_dir), encoding="utf-8")
    monkeypatch.setenv("INFOFORGE_CONFIG", str(path))
    # Never touch podman or the network from a CLI test.
    monkeypatch.setattr(kiwix, "is_up", lambda cfg, session=None: True)
    return path


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


# ------------------------------------------------------------------- basics

def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "infoforge" in capsys.readouterr().out


def test_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0


def test_no_command_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


# ------------------------------------------------------------------ exit codes

def test_missing_config_exits_2_with_a_pointed_message(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("INFOFORGE_CONFIG", str(tmp_path / "absent.yaml"))
    assert cli.main(["ask", "anything"]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "absent.yaml" in err
    assert "config.example.yaml" in err
    assert "Traceback" not in err


def test_ask_ok_exits_0_and_prints_answer_and_citations(config_file, monkeypatch, capsys):
    stub_retrieve(monkeypatch)
    stub_answer(monkeypatch)
    assert cli.main(["ask", "What", "was", "the", "Emu", "War?"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "According to Emu War, 1932." in out
    assert "[1] Emu War — http://127.0.0.1:8080/content/w/Emu_War" in out


def test_ask_without_sources_exits_3(config_file, monkeypatch, capsys):
    stub_retrieve(monkeypatch, sources=[])
    assert cli.main(["ask", "nothing"]) == EXIT_NO_GROUNDING
    assert "no sources found" in capsys.readouterr().err


def test_uncited_answer_warns_but_still_exits_0(config_file, monkeypatch, capsys):
    stub_retrieve(monkeypatch)
    stub_answer(monkeypatch, text="It was 1932.", uncited=True)
    assert cli.main(["ask", "q"]) == EXIT_OK
    assert "WARNING" in capsys.readouterr().out


def test_uncited_answer_exits_4_under_strict(config_file, monkeypatch, capsys):
    stub_retrieve(monkeypatch)
    stub_answer(monkeypatch, text="It was 1932.", uncited=True)
    assert cli.main(["ask", "q", "--strict"]) == EXIT_CITATION
    assert "strict mode" in capsys.readouterr().err


def test_cited_answer_passes_strict(config_file, monkeypatch):
    stub_retrieve(monkeypatch)
    stub_answer(monkeypatch)
    assert cli.main(["ask", "q", "--strict"]) == EXIT_OK


def test_all_llms_down_exits_5_but_still_prints_sources(config_file, monkeypatch,
                                                        capsys):
    stub_retrieve(monkeypatch)

    def boom(cfg, question, sources, session=None):
        raise LLMUnreachableError("every configured LLM was unreachable")
    monkeypatch.setattr(answer_mod, "answer_question", boom)

    assert cli.main(["ask", "q"]) == EXIT_LLM
    captured = capsys.readouterr()
    assert "Emu War" in captured.out  # search alone is still useful
    assert "unreachable" in captured.err


# ----------------------------------------------------------------- json mode

def test_ask_json_is_machine_readable(config_file, monkeypatch, capsys):
    stub_retrieve(monkeypatch)
    stub_answer(monkeypatch)
    assert cli.main(["ask", "q", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["answer"].startswith("According to")
    assert payload["sources"][0]["title"] == "Emu War"


def test_search_json_lists_sources_without_calling_the_llm(config_file, monkeypatch,
                                                           capsys):
    stub_retrieve(monkeypatch)

    def never(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("search must not call the LLM")
    monkeypatch.setattr(answer_mod, "answer_question", never)

    assert cli.main(["search", "emu", "war", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["query"] == "emu war"
    assert payload["sources"][0]["score"] == 12.0
    assert "text" not in payload["sources"][0], "text is opt-in (--text)"


def test_search_json_text_includes_the_extracted_article(config_file, monkeypatch,
                                                         capsys):
    """`--text` hands a caller the article text itself, so it can answer with
    its own model instead of `ask` (DisPatch's Wikipedia bot does this)."""
    stub_retrieve(monkeypatch)
    assert cli.main(["search", "emu", "war", "--json", "--text"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    src = payload["sources"][0]
    assert src["title"] == "Emu War" and src["text"] and src["chars"] == len(src["text"])


def test_search_without_hits_exits_3(config_file, monkeypatch):
    stub_retrieve(monkeypatch, sources=[])
    assert cli.main(["search", "nothing"]) == EXIT_NO_GROUNDING


# --------------------------------------------------------------- other verbs

def test_zim_list_shows_config_and_disk(config_file, capsys):
    assert cli.main(["zim", "list", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["zims"][0]["file"] == "test.zim"
    assert payload["zims"][0]["present"] is True


def test_doctor_reports_instead_of_raising(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("INFOFORGE_CONFIG", str(tmp_path / "absent.yaml"))
    code = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert code == 1
    assert "[FAIL] config" in out
    assert "problems found" in out


def test_doctor_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("INFOFORGE_CONFIG", str(tmp_path / "absent.yaml"))
    cli.main(["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["checks"][0]["name"] == "config"


def test_serve_status_reports_container_state(config_file, monkeypatch, capsys):
    monkeypatch.setattr(kiwix, "status", lambda cfg: {
        "name": "infoforge-kiwix", "state": "running", "kiwix_url": "http://x",
        "configured": ["test.zim"], "serving": ["test.zim"], "ps": "img | Up | 8080",
    })
    assert cli.main(["serve", "status"]) == EXIT_OK
    assert "running" in capsys.readouterr().out


def test_serve_start_reports_the_action(config_file, monkeypatch, capsys):
    monkeypatch.setattr(kiwix, "ensure", lambda cfg: {
        "action": "created", "state": "running", "files": ["test.zim"]})
    assert cli.main(["serve", "start"]) == EXIT_OK
    assert "created" in capsys.readouterr().out


# ------------------------------------------------------------ not in sources

REFUSAL = "Emu War does not say what anyone ate for breakfast."


def test_not_in_sources_exits_6_even_though_it_names_a_title(config_file, monkeypatch,
                                                             capsys):
    stub_retrieve(monkeypatch)
    stub_answer(monkeypatch, text=REFUSAL, verdict="not_in_sources")
    assert cli.main(["ask", "breakfast?"]) == EXIT_NOT_IN_SOURCES
    captured = capsys.readouterr()
    assert REFUSAL in captured.out
    assert "[1] Emu War" in captured.out          # sources still listed
    assert "not in the sources" in captured.err


def test_not_in_sources_exits_6_under_strict_too(config_file, monkeypatch):
    # The cited-check passes (a title is named) — the verdict must still win.
    stub_retrieve(monkeypatch)
    stub_answer(monkeypatch, text=REFUSAL, verdict="not_in_sources")
    assert cli.main(["ask", "breakfast?", "--strict"]) == EXIT_NOT_IN_SOURCES


def test_not_in_sources_json(config_file, monkeypatch, capsys):
    stub_retrieve(monkeypatch)
    stub_answer(monkeypatch, text=REFUSAL, verdict="not_in_sources")
    assert cli.main(["ask", "breakfast?", "--json"]) == EXIT_NOT_IN_SOURCES
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["exit_code"] == EXIT_NOT_IN_SOURCES
    assert payload["reason"] == "not_in_sources"
    assert payload["answer"] == REFUSAL
    assert payload["sources"][0]["title"] == "Emu War"


def test_ok_json_carries_exit_code_and_reason(config_file, monkeypatch, capsys):
    stub_retrieve(monkeypatch)
    stub_answer(monkeypatch)
    assert cli.main(["ask", "q", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == EXIT_OK
    assert payload["reason"] == "answered"
    assert payload["verdict"] == "answered"


def test_exit_3_with_json_still_prints_json(config_file, monkeypatch, capsys):
    stub_retrieve(monkeypatch, sources=[])
    assert cli.main(["ask", "zqxv", "--json"]) == EXIT_NO_GROUNDING
    payload = json.loads(capsys.readouterr().out)
    assert payload == {**payload, "ok": False, "exit_code": EXIT_NO_GROUNDING,
                       "reason": "no_sources", "sources": []}


def test_strict_uncited_json_says_uncited(config_file, monkeypatch, capsys):
    stub_retrieve(monkeypatch)
    stub_answer(monkeypatch, text="It was 1932.", uncited=True)
    assert cli.main(["ask", "q", "--strict", "--json"]) == EXIT_CITATION
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["exit_code"] == EXIT_CITATION
    assert payload["reason"] == "uncited"


def test_llm_down_json_has_exit_code_and_reason(config_file, monkeypatch, capsys):
    stub_retrieve(monkeypatch)

    def boom(cfg, question, sources, session=None):
        raise LLMUnreachableError("every configured LLM was unreachable")
    monkeypatch.setattr(answer_mod, "answer_question", boom)
    assert cli.main(["ask", "q", "--json"]) == EXIT_LLM
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["exit_code"] == EXIT_LLM
    assert payload["reason"] == "llm_unreachable"
    assert payload["sources"][0]["title"] == "Emu War"


def test_config_error_json(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("INFOFORGE_CONFIG", str(tmp_path / "absent.yaml"))
    assert cli.main(["ask", "q", "--json"]) == EXIT_CONFIG
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["exit_code"] == EXIT_CONFIG
    assert payload["reason"] == "config"
    assert payload["sources"] == []
