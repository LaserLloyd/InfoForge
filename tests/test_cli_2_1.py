"""CLI behaviour added in 2.1: query safety, --no-ensure, error objects, config, zim."""

from __future__ import annotations

import json

import pytest
import requests
from conftest import FakeResponse
from helpers import SOURCES, stub_answer, stub_retrieve

from infoforge import answer as answer_mod
from infoforge import api, cli, kiwix, retrieve
from infoforge.config import (
    EXIT_CONFIG,
    EXIT_NO_GROUNDING,
    EXIT_OK,
    InfoForgeError,
)

# ----------------------------------------------------- question option safety

@pytest.mark.parametrize("argv,expected", [
    # nothing to protect
    (["status"], ["status"]),
    (["--config", "c.yaml", "doctor"], ["--config", "c.yaml", "doctor"]),
    # plain question, flags before and after it
    (["ask", "what", "is", "x"], ["ask", "--", "what", "is", "x"]),
    (["ask", "--json", "q"], ["ask", "--json", "--", "q"]),
    (["ask", "q", "--json"], ["ask", "--json", "--", "q"]),
    (["ask", "--max-articles", "2", "q"], ["ask", "--max-articles", "2", "--", "q"]),
    (["ask", "--max-articles=2", "q"], ["ask", "--max-articles=2", "--", "q"]),
    (["search", "--text", "--no-ensure", "--json", "q"],
     ["search", "--text", "--no-ensure", "--json", "--", "q"]),
    # unknown dash-words belong to the question
    (["search", "-5", "degrees", "in", "kelvin?"],
     ["search", "--", "-5", "degrees", "in", "kelvin?"]),
    (["search", "--version"], ["search", "--", "--version"]),
    (["search", "--config=evil.yaml", "x"], ["search", "--", "--config=evil.yaml", "x"]),
    (["ask", "--strict", "-x", "--json", "y"], ["ask", "--strict", "--json", "--", "-x", "y"]),
    # --strict is not a search flag, so it is part of a search query
    (["search", "--strict", "q"], ["search", "--", "--strict", "q"]),
    # an explicit -- ends flag parsing; everything after it is the question
    (["search", "--json", "--", "--json", "-1"], ["search", "--json", "--", "--json", "-1"]),
    # a flag given a value form it does not take is question text
    (["search", "--json=1", "q"], ["search", "--", "--json=1", "q"]),
    # a trailing value-flag with no value is left for argparse to reject
    (["ask", "q", "--max-articles"], ["ask", "--max-articles", "--", "q"]),
    # global options before the subcommand are skipped
    (["--config", "c.yaml", "search", "-1"], ["--config", "c.yaml", "search", "--", "-1"]),
    (["--config=c.yaml", "search", "-1"], ["--config=c.yaml", "search", "--", "-1"]),
])
def test_protect_query(argv, expected):
    assert cli.protect_query(argv) == expected


def test_protect_query_with_only_global_flags_is_untouched():
    assert cli.protect_query(["--version"]) == ["--version"]
    assert cli.protect_query([]) == []


@pytest.mark.parametrize("query", [
    ["-5", "degrees", "in", "kelvin?"],
    ["--version"],
    ["--config=other.yaml", "x"],
    ["-x"],
])
def test_a_question_starting_with_a_dash_is_searched_not_parsed(
        query, config_file, monkeypatch, capsys):
    seen = {}

    def fake(cfg, q, max_articles=None, session=None):
        seen["q"] = q
        return list(SOURCES)
    monkeypatch.setattr(retrieve, "retrieve", fake)
    assert cli.main(["search", "--json", *query]) == EXIT_OK
    assert seen["q"] == " ".join(query)
    assert json.loads(capsys.readouterr().out)["query"] == " ".join(query)


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_is_still_a_flag_of_the_subcommand(flag, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["search", flag])
    assert exc.value.code == 0
    assert "--no-ensure" in capsys.readouterr().out


def test_an_option_like_question_does_not_repoint_the_config(config_file, monkeypatch,
                                                              capsys, tmp_path):
    stub_retrieve(monkeypatch)
    assert cli.main(["search", "--json", f"--config={tmp_path}/evil.yaml", "x"]) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["query"].startswith("--config=")


def test_double_dash_is_accepted_for_ask_and_search(config_file, monkeypatch, capsys):
    stub_retrieve(monkeypatch)
    stub_answer(monkeypatch)
    assert cli.main(["ask", "--json", "--", "-5 degrees in kelvin?"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["question"] == "-5 degrees in kelvin?"
    assert cli.main(["search", "--", "-5", "degrees"]) == EXIT_OK


def test_flags_still_work_after_the_question(config_file, monkeypatch, capsys):
    # README: infoforge ask "<the question>" --json
    stub_retrieve(monkeypatch)
    stub_answer(monkeypatch)
    assert cli.main(["ask", "What was the Emu War?", "--json", "--strict"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_abbreviated_flags_are_not_accepted():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--conf", "x", "status"])


def test_empty_query_is_a_usage_error(config_file, capsys):
    with pytest.raises(SystemExit) as exc:   # argparse: question is required
        cli.main(["search"])
    assert exc.value.code == 2
    assert cli.main(["search", "   "]) == EXIT_CONFIG
    assert "needs a query" in capsys.readouterr().err
    assert cli.main(["ask", "  ", "--json"]) == EXIT_CONFIG
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "usage_error"


# ------------------------------------------------------------- --no-ensure

def test_search_no_ensure_fails_fast_with_a_stable_error(config_file, monkeypatch,
                                                         capsys):
    monkeypatch.setattr(kiwix, "is_up", lambda cfg, session=None, timeout=5: False)
    monkeypatch.setattr(kiwix, "ensure", lambda cfg: pytest.fail("must not create"))
    monkeypatch.setattr(kiwix, "_podman", lambda *a, **k: pytest.fail("must not run podman"))
    assert cli.main(["search", "--no-ensure", "--json", "emu"]) == EXIT_CONFIG
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False and payload["exit_code"] == 2 and payload["reason"] == "config"
    assert payload["error"]["code"] == "kiwix_unreachable"
    assert payload["error"]["fix"] and payload["error"]["docs"].endswith("#kiwix_unreachable")
    assert payload["sources"] == [] and payload["query"] == "emu"
    assert "kiwix-serve does not answer" in captured.err


def test_search_without_the_flag_may_still_ensure(config_file, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(api, "ensure_kiwix", lambda c, session=None, notify=None: seen.append(1))
    stub_retrieve(monkeypatch)
    assert cli.main(["search", "emu"]) == EXIT_OK
    assert seen == [1]
    assert cli.main(["search", "--no-ensure", "emu"]) == EXIT_OK
    assert seen == [1]


def test_search_json_failure_for_no_sources_is_json_too(config_file, monkeypatch, capsys):
    stub_retrieve(monkeypatch, sources=[])
    assert cli.main(["search", "--json", "zqxv"]) == EXIT_NO_GROUNDING
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "no_sources" and payload["error"]["code"] == "no_sources"


# ------------------------------------------------- error object / attribution

def test_ask_json_errors_carry_code_fix_docs(config_file, monkeypatch, capsys):
    stub_retrieve(monkeypatch, sources=[])
    assert cli.main(["ask", "zqxv", "--json"]) == EXIT_NO_GROUNDING
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["error"]) == {"code", "message", "fix", "docs"}
    assert payload["error"]["code"] == "no_sources"
    assert payload["exit_code"] == 3 and payload["reason"] == "no_sources"


def test_missing_config_json_has_a_config_missing_code(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("INFOFORGE_CONFIG", str(tmp_path / "absent.yaml"))
    assert cli.main(["ask", "q", "--json"]) == EXIT_CONFIG
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "config_missing"
    assert "config.example.yaml" in payload["error"]["message"]


def test_ask_text_prints_the_attribution_line(config_file, monkeypatch, capsys):
    monkeypatch.setattr(retrieve, "retrieve", lambda *a, **k: [retrieve.Source(
        title="Paris", url="http://127.0.0.1:18080/content/wikipedia_en_x/A/Paris",
        text="x", score=1.0)])
    stub_answer(monkeypatch, text="According to Paris, yes.")
    # test.zim is not a Wikipedia-family name, so the registry says unknown ...
    assert cli.main(["ask", "q"]) == EXIT_OK
    assert "Attribution:" in capsys.readouterr().out


def test_ask_text_attribution_for_a_wikipedia_zim(tmp_path, monkeypatch, capsys):
    zim_dir = tmp_path / "zims"
    zim_dir.mkdir()
    path = tmp_path / "c.yaml"
    path.write_text(
        "model_id: m\nbase_url: http://127.0.0.1:11234/v1\n"
        f"zim_dir: {zim_dir.as_posix()}\nzims:\n  - file: wikipedia_en_x.zim\n",
        encoding="utf-8")
    monkeypatch.setenv("INFOFORGE_CONFIG", str(path))
    monkeypatch.setattr(kiwix, "is_up", lambda cfg, session=None, timeout=5: True)
    monkeypatch.setattr(retrieve, "retrieve", lambda *a, **k: [retrieve.Source(
        title="Paris", url="http://127.0.0.1:18080/content/wikipedia_en_x/A/Paris",
        text="x", score=1.0)])
    stub_answer(monkeypatch, text="According to Paris, yes.")
    assert cli.main(["ask", "paris"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Attribution: Paris: from Wikipedia and sister projects, licensed CC BY-SA 4.0" in out
    assert "[1] Paris" in out
    assert cli.main(["ask", "paris", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["sources"][0]["license"] == "CC BY-SA 4.0"
    assert "CC BY-SA 4.0" in payload["attribution"]


# ------------------------------------------------------------------- config

def test_config_show_text_and_json(config_file, capsys):
    assert cli.main(["config", "show"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "model_id" in out and "[file]" in out and "[default]" in out
    assert cli.main(["config", "show", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["settings"]) == 17 and payload["schema_version"] == 1


def test_config_show_with_env_only_names_no_file(monkeypatch, capsys):
    monkeypatch.setenv("INFOFORGE_MODEL_ID", "m")
    monkeypatch.setenv("INFOFORGE_BASE_URL", "http://127.0.0.1:11234/v1")
    monkeypatch.setenv("INFOFORGE_CONFIG", "does-not-exist.yaml")
    assert cli.main(["config", "show"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "environment only" in out and "[env]" in out


def test_config_show_never_prints_the_api_key(tmp_path, monkeypatch, capsys):
    path = tmp_path / "c.yaml"
    path.write_text("model_id: m\nbase_url: http://127.0.0.1:11234/v1\n"
                    "api_key: sk-very-secret\n", encoding="utf-8")
    monkeypatch.setenv("INFOFORGE_CONFIG", str(path))
    cli.main(["config", "show"])
    cli.main(["config", "show", "--json"])
    assert "sk-very-secret" not in capsys.readouterr().out


def test_config_schema_json_and_text(capsys):
    assert cli.main(["config", "schema", "--json"]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert doc["properties"]["kiwix_port"]["default"] == 8090
    assert cli.main(["config", "schema"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "INFOFORGE_KIWIX_PORT" in out and "kiwix_image_tag" in out


def test_config_schema_needs_no_config_file(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("INFOFORGE_CONFIG", str(tmp_path / "absent.yaml"))
    assert cli.main(["config", "schema", "--json"]) == EXIT_OK


# --------------------------------------------------------- status / zim / serve

def test_status_json_without_podman(config_file, monkeypatch, capsys):
    def no_podman(cfg):
        raise kiwix.KiwixError("x", code="podman_missing")
    monkeypatch.setattr(kiwix, "status", no_podman)
    assert cli.main(["status", "--json"]) == EXIT_OK
    info = json.loads(capsys.readouterr().out)
    assert info["state"] == "unavailable" and info["kiwix_responding"] is True
    assert cli.main(["status"]) == EXIT_OK
    assert "container infoforge-kiwix: unavailable" in capsys.readouterr().out


def test_zim_list_text(config_file, capsys):
    assert cli.main(["zim", "list"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "zim_dir:" in out and "test.zim  [enabled]" in out


def test_zim_list_text_empty(tmp_path, monkeypatch, capsys):
    path = tmp_path / "c.yaml"
    path.write_text(f"model_id: m\nbase_url: http://127.0.0.1:11234/v1\n"
                    f"zim_dir: {(tmp_path / 'none').as_posix()}\n", encoding="utf-8")
    monkeypatch.setenv("INFOFORGE_CONFIG", str(path))
    assert cli.main(["zim", "list"]) == EXIT_OK
    assert "no zims configured" in capsys.readouterr().out


def test_zim_list_flags_a_missing_file(tmp_path, monkeypatch, capsys):
    path = tmp_path / "c.yaml"
    path.write_text(f"model_id: m\nbase_url: http://127.0.0.1:11234/v1\n"
                    f"zim_dir: {tmp_path.as_posix()}\nzims:\n  - file: gone.zim\n",
                    encoding="utf-8")
    monkeypatch.setenv("INFOFORGE_CONFIG", str(path))
    cli.main(["zim", "list"])
    assert "MISSING on disk" in capsys.readouterr().out


CATALOG_XML = ("<feed><entry><name>wikipedia_en_x_2026-09</name></entry></feed>")


@pytest.fixture
def dated_zim_config(tmp_path, monkeypatch):
    path = tmp_path / "c.yaml"
    path.write_text(
        "model_id: m\nbase_url: http://127.0.0.1:11234/v1\n"
        f"zim_dir: {tmp_path.as_posix()}\nzims:\n"
        "  - file: wikipedia_en_x_2026-01.zim\n"
        "  - file: wikipedia_en_x_2026-09.zim\n"
        "  - file: undated.zim\n",
        encoding="utf-8")
    monkeypatch.setenv("INFOFORGE_CONFIG", str(path))


def test_zim_check_update_text(dated_zim_config, monkeypatch, capsys):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(200, CATALOG_XML))
    assert cli.main(["zim", "check-update"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "NEWER dump available (2026-09, local 2026-01)" in out
    assert "wikipedia_en_x_2026-09.zim: up to date" in out
    assert "undated.zim: no catalog entry found" in out
    assert "report only" in out


def test_zim_check_update_reports_errors_and_json(dated_zim_config, monkeypatch, capsys):
    def down(*a, **k):
        raise requests.exceptions.ConnectionError("offline")
    monkeypatch.setattr(requests, "get", down)
    assert cli.main(["zim", "check-update"]) == EXIT_OK
    assert "catalog unreachable" in capsys.readouterr().out
    assert cli.main(["zim", "check-update", "--json"]) == EXIT_OK
    assert len(json.loads(capsys.readouterr().out)["checked"]) == 3


def test_zim_check_update_with_nothing_configured(config_file, monkeypatch, capsys):
    monkeypatch.setattr(api, "zim_check_update", lambda **k: {"checked": []})
    assert cli.main(["zim", "check-update"]) == EXIT_OK
    assert "no zims configured to check" in capsys.readouterr().out


def test_serve_stop_and_recreate(config_file, monkeypatch, capsys):
    monkeypatch.setattr(kiwix, "stop", lambda cfg: {"action": "stopped", "state": "exited"})
    monkeypatch.setattr(kiwix, "recreate", lambda cfg: ["test.zim"])
    assert cli.main(["serve", "stop"]) == EXIT_OK
    assert "exited (stopped)" in capsys.readouterr().out
    assert cli.main(["serve", "recreate"]) == EXIT_OK
    assert "recreated" in capsys.readouterr().out


# ------------------------------------------------------------------- doctor

def test_doctor_text_exit_codes(config_file, monkeypatch, capsys):
    monkeypatch.setattr(kiwix, "podman_available", lambda: False)
    monkeypatch.setattr(kiwix, "search", lambda *a, **k: [object()])

    class Ok:
        status_code = 200

        def json(self):
            return {"data": [{"id": "test-model"}]}
    monkeypatch.setattr(requests, "get", lambda *a, **k: Ok())
    # fallbacks absent; primary reachable; kiwix is_up patched True by the fixture
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "all checks passed" in out and "[PASS] container" in out
    assert cli.main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


# ------------------------------------------------------------------- zimify

def test_zimify_prints_the_manual_command_without_zimwriterfs(tmp_path, monkeypatch, capsys):
    from infoforge import cli as cli_mod
    calls = {}

    def fake_zimify(input_dir, output, **kw):
        calls.update(input_dir=input_dir, output=output, **kw)
        return {"built": False, "prep_dir": str(tmp_path / "prep"),
                "command": ["zimwriterfs", "--title", "My Notes"], "files": 2}
    monkeypatch.setattr(cli_mod, "zimify", fake_zimify)
    notes = tmp_path / "notes"
    notes.mkdir()
    assert cli.main(["--config", str(tmp_path / "absent.yaml"), "zimify", str(notes),
                     "--title", "My Notes"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "was NOT built" in out and "'My Notes'" in out
    assert calls["output"].name == "notes.zim"


def test_zimify_built(tmp_path, monkeypatch, capsys):
    from infoforge import cli as cli_mod
    monkeypatch.setattr(cli_mod, "zimify", lambda i, o, **kw: {
        "built": True, "zim": str(o), "prep_dir": "prep", "files": 3, "command": []})
    assert cli.main(["--config", str(tmp_path / "absent.yaml"), "zimify", str(tmp_path),
                     "-o", str(tmp_path / "x.zim")]) == EXIT_OK
    out = capsys.readouterr().out
    assert "built" in out and "serve recreate" in out


# --------------------------------------------------------------- main() edges

def test_keyboard_interrupt_exits_130(config_file, monkeypatch, capsys):
    def boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(api, "search", boom)
    assert cli.main(["search", "x"]) == 130
    assert "interrupted" in capsys.readouterr().err


def test_main_without_argv_reads_sys_argv(config_file, monkeypatch, capsys):
    stub_retrieve(monkeypatch)
    monkeypatch.setattr("sys.argv", ["infoforge", "search", "-5", "x"])
    assert cli.main() == EXIT_OK


def test_infoforge_error_in_a_command_maps_to_its_exit_code(config_file, monkeypatch, capsys):
    def boom(*a, **k):
        raise InfoForgeError("kaboom")
    monkeypatch.setattr(api, "zim_list", boom)
    assert cli.main(["zim", "list"]) == EXIT_CONFIG
    assert "kaboom" in capsys.readouterr().err


def test_version_string_is_the_package_version(capsys):
    from infoforge import __version__
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert f"infoforge {__version__}" in capsys.readouterr().out
    assert __version__ == "2.1.0"


def test_answer_module_is_the_one_the_api_calls():
    # The tests above monkeypatch answer_mod.answer_question; api must look it up late.
    assert api.answer_mod is answer_mod


def test_cli_max_articles_below_one_is_exit_2(config_file, capsys):
    # review fix (opus): `--max-articles -1` used to search for nothing (exit 3).
    assert cli.main(["search", "--json", "--max-articles", "-1", "emu"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == "usage_error"
