"""The library API: the same pipeline the CLI prints, as dicts."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import requests
from conftest import FakeResponse

from infoforge import answer as answer_mod
from infoforge import api, cli, kiwix, retrieve
from infoforge.answer import Answer
from infoforge.config import Endpoint, LLMUnreachableError, Zim
from infoforge.errors import InfoForgeError
from infoforge.retrieve import Source

GOLDEN = Path(__file__).parent / "golden"

SOURCES = [
    Source(title="Emu War", url="http://127.0.0.1:18080/content/w/Emu_War",
           text="1932 operation.", score=12.0),
    Source(title='Émeu é "quoted"', url="http://127.0.0.1:18080/content/w/X",
           text="line1\nline2 — dash", score=3.25),
]


@pytest.fixture
def up(monkeypatch):
    calls = []

    def is_up(cfg, session=None, timeout=5):
        calls.append(timeout)
        return True
    monkeypatch.setattr(kiwix, "is_up", is_up)
    return calls


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(
        retrieve, "retrieve",
        lambda cfg, q, max_articles=None, session=None: list(SOURCES))


def fake_answer(monkeypatch, text="According to Emu War, 1932.", cited=("Emu War",),
                verdict="answered"):
    def fake(cfg, question, sources, session=None):
        return Answer(text=text, endpoint=Endpoint("test-model", "http://x/v1"),
                      cited=list(cited), uncited=not cited, verdict=verdict,
                      verdict_source="model")
    monkeypatch.setattr(answer_mod, "answer_question", fake)


# ------------------------------------------------------- search: golden bytes

@pytest.mark.parametrize("name,argv", [
    ("search_json", ["search", "--json", "--no-ensure", "emu", "war"]),
    ("search_json_text", ["search", "--json", "--text", "--no-ensure", "emu", "war"]),
    ("search_plain", ["search", "--no-ensure", "emu", "war"]),
])
def test_search_output_is_byte_identical_to_2_0(name, argv, up, stub, capsys,
                                                monkeypatch, tmp_path):
    # The golden files were produced by running the unmodified 2.0 CLI on the
    # same stubbed sources; DisPatch parses this document, so it must not move.
    path = tmp_path / "c.yaml"
    path.write_text(f"model_id: m\nbase_url: http://127.0.0.1:11234/v1\n"
                    f"zim_dir: {tmp_path.as_posix()}\n", encoding="utf-8")
    monkeypatch.setenv("INFOFORGE_CONFIG", str(path))
    assert cli.main(argv) == 0
    expected = (GOLDEN / f"{name}.txt").read_bytes().decode("utf-8")
    assert capsys.readouterr().out == expected


def test_search_ensure_flag_selects_the_path(cfg, monkeypatch, stub):
    seen = []
    monkeypatch.setattr(api, "ensure_kiwix",
                        lambda c, session=None, notify=None: seen.append("ensure"))
    monkeypatch.setattr(api, "require_kiwix", lambda c, session=None: seen.append("require"))
    api.search("emu war", cfg=cfg, ensure=True)
    api.search("emu war", cfg=cfg)
    assert seen == ["ensure", "require"]


def test_search_returns_the_documented_shape(cfg, up, stub):
    result = api.search("  emu war ", cfg=cfg)
    assert list(result) == ["query", "sources"]
    assert result["query"] == "emu war"
    assert list(result["sources"][0]) == ["title", "url", "score", "chars"]
    with_text = api.search("emu war", cfg=cfg, text=True)
    assert with_text["sources"][0]["text"] == "1932 operation."


def test_search_needs_a_query(cfg, up):
    with pytest.raises(InfoForgeError) as exc:
        api.search("   ", cfg=cfg)
    assert exc.value.code == "usage_error"


def test_search_without_hits_is_no_sources(cfg, up, monkeypatch):
    monkeypatch.setattr(retrieve, "retrieve", lambda *a, **k: [])
    with pytest.raises(InfoForgeError) as exc:
        api.search("zqxv", cfg=cfg)
    assert exc.value.exit_code == 3 and exc.value.code == "no_sources"


# ---------------------------------------------------- no-ensure: fail fast

def test_no_ensure_fails_fast_without_touching_podman(cfg, monkeypatch):
    touched = []
    monkeypatch.setattr(kiwix, "_podman", lambda *a, **k: touched.append("podman"))
    monkeypatch.setattr(kiwix, "ensure", lambda *a, **k: touched.append("ensure"))

    class Down:
        def get(self, *a, **k):
            raise requests.exceptions.ConnectionError("refused")
    with pytest.raises(kiwix.KiwixError) as exc:
        api.search("emu war", cfg=cfg, session=Down())
    assert exc.value.code == "kiwix_unreachable" and exc.value.exit_code == 2
    assert touched == []
    assert "infoforge serve start" in str(exc.value)


def test_no_ensure_probe_is_short(cfg, stub):
    seen = {}

    class S:
        def get(self, url, timeout=None, **k):
            seen["timeout"] = timeout
            return FakeResponse(200)
    api.search("emu war", cfg=cfg, session=S())
    assert seen["timeout"] <= 2.0, "must stay well inside an 8 s caller's budget"


def test_ensure_without_podman_is_kiwix_unreachable(cfg, monkeypatch):
    monkeypatch.setattr(kiwix, "is_up", lambda *a, **k: False)
    monkeypatch.setattr(kiwix, "podman_available", lambda: False)
    with pytest.raises(kiwix.KiwixError) as exc:
        api.ensure_kiwix(cfg)
    assert exc.value.code == "kiwix_unreachable"
    assert "podman is not installed" in str(exc.value)


def test_ensure_starts_the_container_then_waits_for_http(cfg, monkeypatch):
    answers = iter([False, False, True])
    monkeypatch.setattr(kiwix, "is_up", lambda *a, **k: next(answers))
    monkeypatch.setattr(kiwix, "podman_available", lambda: True)
    monkeypatch.setattr(kiwix, "ensure", lambda c: {
        "action": "created", "state": "running", "files": ["a.zim"]})
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    notes = []
    api.ensure_kiwix(cfg, notify=notes.append)
    assert notes == ["kiwix container created: serving a.zim"]


def test_ensure_gives_up_when_nothing_ever_answers(cfg, monkeypatch):
    monkeypatch.setattr(kiwix, "is_up", lambda *a, **k: False)
    monkeypatch.setattr(kiwix, "podman_available", lambda: True)
    monkeypatch.setattr(kiwix, "ensure", lambda c: {
        "action": "none", "state": "running", "files": ["a.zim"]})
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    with pytest.raises(kiwix.KiwixError, match="nothing answers") as exc:
        api.ensure_kiwix(cfg)
    assert exc.value.code == "kiwix_unreachable"


def test_ensure_is_a_noop_when_kiwix_already_answers(cfg, up, monkeypatch):
    monkeypatch.setattr(kiwix, "ensure", lambda c: pytest.fail("must not touch podman"))
    api.ensure_kiwix(cfg)


# ----------------------------------------------------------------------- ask

def test_ask_success_envelope_has_licenses_and_attribution(cfg, up, stub, monkeypatch):
    fake_answer(monkeypatch)
    cfg2 = replace(cfg, zims=(Zim(file="w.zim", name="W"),))  # not a Wikipedia-family name
    out = api.ask("what was the emu war", cfg=cfg2, ensure=False)
    assert out["ok"] is True and out["exit_code"] == 0 and out["reason"] == "answered"
    assert out["answer"].startswith("According to")
    assert out["sources"][0]["license"] == "unknown"
    assert "licence unknown" in out["attribution"]
    assert "error" not in out


def test_ask_attributes_wikipedia_as_cc_by_sa(cfg, up, monkeypatch):
    monkeypatch.setattr(retrieve, "retrieve", lambda *a, **k: [Source(
        title="Paris", url="http://127.0.0.1:18080/content/wikipedia_en_all_nopic/A/Paris",
        text="x", score=1.0)])
    fake_answer(monkeypatch, text="According to Paris, yes.", cited=("Paris",))
    out = api.ask("paris?", cfg=cfg, ensure=False)
    assert out["sources"][0]["license"] == "CC BY-SA 4.0"
    assert "CC BY-SA 4.0" in out["attribution"]
    assert "https://creativecommons.org/licenses/by-sa/4.0/" in out["attribution"]
    assert "Paris" in out["attribution"]


def test_attribution_names_each_licence(cfg):
    cfg2 = replace(cfg, zims=(Zim(file="wikipedia_a.zim"),
                              Zim(file="notes.zim", license="CC0"),
                              Zim(file="mystery.zim")))
    rows = [{"title": "T1", "url": "http://h/content/wikipedia_a/A/T1"},
            {"title": "T2", "url": "http://h/content/notes/A/T2"},
            {"title": "T3", "url": "http://h/content/mystery/A/T3"}]
    line = api.attribution_for(cfg2, rows)
    assert "T1: from Wikipedia" in line and "T2: licensed CC0." in line
    assert "T3: licence unknown" in line


def test_ask_every_outcome_is_an_envelope(cfg, up, stub, monkeypatch):
    fake_answer(monkeypatch, text="Emu War does not say.", verdict="not_in_sources")
    out = api.ask("breakfast?", cfg=cfg, ensure=False)
    assert (out["ok"], out["exit_code"], out["reason"]) == (False, 6, "not_in_sources")
    assert out["error"]["code"] == "not_in_sources" and out["error"]["fix"]
    assert out["sources"]

    fake_answer(monkeypatch, text="It was 1932.", cited=())
    out = api.ask("q", cfg=cfg, ensure=False, strict=True)
    assert (out["exit_code"], out["reason"], out["error"]["code"]) == (4, "uncited", "uncited")
    assert api.ask("q", cfg=cfg, ensure=False)["exit_code"] == 0  # not strict

    def boom(*a, **k):
        raise LLMUnreachableError("all down")
    monkeypatch.setattr(answer_mod, "answer_question", boom)
    out = api.ask("q", cfg=cfg, ensure=False)
    assert (out["exit_code"], out["reason"]) == (5, "llm_unreachable")
    assert out["error"]["message"] == "all down" and out["sources"]


def test_ask_failures_before_retrieval_are_envelopes_too(cfg, monkeypatch, tmp_path):
    out = api.ask("", cfg=cfg, ensure=False)
    assert out["exit_code"] == 2 and out["error"]["code"] == "usage_error"
    out = api.ask("q", config=str(tmp_path / "absent.yaml"))
    assert out["exit_code"] == 2 and out["reason"] == "config"
    assert out["error"]["code"] == "config_missing" and out["sources"] == []
    monkeypatch.setattr(kiwix, "is_up", lambda *a, **k: False)
    out = api.ask("q", cfg=cfg, ensure=False)
    assert out["error"]["code"] == "kiwix_unreachable"
    monkeypatch.setattr(kiwix, "is_up", lambda *a, **k: True)
    monkeypatch.setattr(retrieve, "retrieve", lambda *a, **k: [])
    out = api.ask("zqxv", cfg=cfg, ensure=False)
    assert (out["exit_code"], out["error"]["code"]) == (3, "no_sources")


def test_ask_notifies_progress_and_logs_hashes_not_questions(cfg, up, stub, monkeypatch,
                                                              tmp_path):
    fake_answer(monkeypatch)
    notes = []
    api.ask("the secret question", cfg=cfg, ensure=False, notify=notes.append)
    assert notes == ["grounding on 2 article(s)"]
    log = (tmp_path / "log.jsonl").read_text(encoding="utf-8")
    assert "secret" not in log and "question_sha256" in log
    assert "Emu War" not in log  # titles reveal the topic too


def test_ask_logs_text_when_opted_in(cfg, up, stub, monkeypatch, tmp_path):
    fake_answer(monkeypatch)
    api.ask("the secret question", cfg=replace(cfg, log_questions=True), ensure=False)
    log = (tmp_path / "log.jsonl").read_text(encoding="utf-8")
    assert "the secret question" in log and "Emu War" in log


# ------------------------------------------------------- status / zim / doctor

def test_status_without_podman_reports_unavailable(cfg, up, monkeypatch):
    def no_podman(*a, **k):
        raise kiwix.KiwixError("no podman", code="podman_missing")
    monkeypatch.setattr(kiwix, "status", no_podman)
    info = api.status(cfg=cfg)
    assert info["state"] == "unavailable" and info["podman"] is False
    assert info["kiwix_responding"] is True
    assert info["configured"] == ["wikipedia_en_all_nopic.zim"]


def test_status_reraises_other_container_errors(cfg, monkeypatch):
    monkeypatch.setattr(kiwix, "podman_available", lambda: True)
    def broken(*a, **k):
        raise kiwix.KiwixError("podman exploded", code="container_failed")
    monkeypatch.setattr(kiwix, "status", broken)
    with pytest.raises(kiwix.KiwixError):
        api.status(cfg=cfg)


def test_status_with_podman_adds_the_flag(cfg, up, monkeypatch):
    monkeypatch.setattr(kiwix, "podman_available", lambda: True)
    monkeypatch.setattr(kiwix, "status", lambda c: {
        "name": "infoforge-kiwix", "state": "running", "kiwix_url": c.kiwix_url,
        "configured": [], "serving": []})
    info = api.status(cfg=cfg)
    assert info["podman"] is True and info["kiwix_responding"] is True


def test_zim_list_marks_missing_and_unregistered(cfg):
    (cfg.zim_dir / "extra.zim").write_bytes(b"x")
    cfg2 = replace(cfg, zims=cfg.zims + (Zim(file="gone.zim", name="Gone"),))
    rows = {r["file"]: r for r in api.zim_list(cfg=cfg2)["zims"]}
    assert rows["wikipedia_en_all_nopic.zim"]["present"] is True
    assert rows["gone.zim"]["present"] is False
    assert rows["extra.zim"]["name"] == "(not in config)"


CATALOG_XML = ("<feed xmlns='http://www.w3.org/2005/Atom'>"
               "<entry><name>wikipedia_en_all_nopic_2026-09</name></entry>"
               "<entry><name>wikipedia_en_all_nopic_2025-01</name></entry></feed>")


def test_zim_check_update_reports_a_newer_dump(cfg, monkeypatch):
    cfg2 = replace(cfg, zims=(Zim(file="wikipedia_en_all_nopic_2026-01.zim"),))
    seen = {}

    def get(url, params=None, timeout=None):
        seen.update(url=url, params=params)
        return FakeResponse(200, CATALOG_XML)
    monkeypatch.setattr(requests, "get", get)
    row = api.zim_check_update(cfg=cfg2)["checked"][0]
    assert row["latest"] == "2026-09" and row["local_date"] == "2026-01"
    assert row["newer_available"] is True and row["error"] is None
    assert seen["url"] == api.CATALOG_URL
    assert seen["params"]["q"] == "wikipedia_en_all_nopic"


def test_zim_check_update_survives_a_dead_catalog_and_bad_xml(cfg, monkeypatch):
    def down(*a, **k):
        raise requests.exceptions.ConnectionError("offline")
    monkeypatch.setattr(requests, "get", down)
    assert "catalog unreachable" in api.zim_check_update(cfg=cfg)["checked"][0]["error"]
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(200, "<not xml"))
    assert "not XML" in api.zim_check_update(cfg=cfg)["checked"][0]["error"]


def test_latest_catalog_date_ignores_other_families():
    xml = ("<feed><entry><name>other_2030-01</name></entry>"
           "<entry><name>fam_2024-05</name></entry></feed>")
    assert api.latest_catalog_date(xml, "fam") == "2024-05"
    assert api.latest_catalog_date(xml, "nothing") is None


def test_doctor_returns_a_plain_dict(tmp_path):
    out = api.doctor(config=str(tmp_path / "absent.yaml"))
    assert out["ok"] is False and out["checks"][0]["name"] == "config"
    assert out["error"]["code"] == "doctor_failed"
    json.dumps(out)


def test_config_show_and_schema_are_json_ready(cfg):
    assert len(api.config_show(cfg=cfg)["settings"]) == 17
    assert len(api.config_schema()["properties"]) == 17
