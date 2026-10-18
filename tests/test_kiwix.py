"""kiwix client: XML parsing, URL handling, suggest, fetch fallbacks.

Every HTTP call goes through an injected fake session — no network, no podman.
"""

from __future__ import annotations

import requests
from conftest import FakeResponse, search_xml

from infoforge import kiwix


class FakeSession:
    """Records requests and replays queued responses (or raises them)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append({"url": url, "params": params or {}})
        item = self._responses.pop(0) if self._responses else FakeResponse(404)
        if isinstance(item, Exception):
            raise item
        return item

    post = get


def test_parse_search_xml_reads_rss_items():
    xml = search_xml(("Emu War", "/content/wiki/Emu_War", "a 1932 wildlife cull"))
    hits = kiwix.parse_search_xml(xml)
    assert len(hits) == 1
    assert hits[0].title == "Emu War"
    assert hits[0].url == "/content/wiki/Emu_War"
    assert hits[0].snippet


def test_parse_search_xml_accepts_atom_entries():
    xml = ("<feed xmlns='http://www.w3.org/2005/Atom'>"
           "<entry><title>Solar System</title>"
           "<link href='/content/wiki/Solar_System'/></entry></feed>")
    hits = kiwix.parse_search_xml(xml)
    assert [h.title for h in hits] == ["Solar System"]
    assert hits[0].url == "/content/wiki/Solar_System"


def test_parse_search_xml_survives_garbage():
    assert kiwix.parse_search_xml("<not xml at all") == []


def test_search_asks_for_xml_and_scopes_to_one_book(cfg):
    # format=json returns the HTML search page on this kiwix build, and an
    # unscoped query 400s once two books are loaded — both must be pinned.
    session = FakeSession([FakeResponse(200, search_xml(("A", "/content/b/A", "")))])
    kiwix.search(cfg, "wikipedia_en_all_nopic", "emu war", 15, session=session)
    params = session.calls[0]["params"]
    assert params["format"] == "xml"
    assert params["books.name"] == "wikipedia_en_all_nopic"
    assert params["pageLength"] == "15"


def test_search_returns_empty_on_transport_error(cfg):
    session = FakeSession([requests.exceptions.ConnectionError("down")])
    assert kiwix.search(cfg, "book", "x", 5, session=session) == []


def test_search_returns_empty_on_http_error(cfg):
    session = FakeSession([FakeResponse(400, "bad request")])
    assert kiwix.search(cfg, "book", "x", 5, session=session) == []


def test_suggest_matches_ignoring_punctuation(cfg):
    payload = [
        {"kind": "pattern", "value": "containing 'nineteen'", "path": ""},
        {"kind": "path", "value": "Nineteen Eighty-Four", "path": "Nineteen_Eighty-Four"},
    ]
    session = FakeSession([FakeResponse(200, json_data=payload)])
    hit = kiwix.suggest(cfg, "book", "Nineteen Eighty Four", session=session)
    assert hit is not None
    assert hit.title == "Nineteen Eighty-Four"
    assert hit.url == "/content/book/Nineteen_Eighty-Four"
    assert hit.from_suggest is True


def test_suggest_ignores_pattern_pseudo_results(cfg):
    payload = [{"kind": "pattern", "value": "Emu War", "path": "search"}]
    session = FakeSession([FakeResponse(200, json_data=payload)])
    assert kiwix.suggest(cfg, "book", "Emu War", session=session) is None


def test_suggest_requires_an_exact_title(cfg):
    payload = [{"kind": "path", "value": "Emu War aftermath", "path": "X"}]
    session = FakeSession([FakeResponse(200, json_data=payload)])
    assert kiwix.suggest(cfg, "book", "Emu War", session=session) is None


def test_split_content_path_handles_encoding():
    book, parts = kiwix.split_content_path("/content/wiki/Emu%20War")
    assert book == "wiki"
    assert parts == ["Emu%20War"]  # decoded then re-encoded exactly once


def test_split_content_path_accepts_absolute_urls():
    book, parts = kiwix.split_content_path("http://host:8080/content/wiki/A/B")
    assert (book, parts) == ("wiki", ["A", "B"])


def test_split_content_path_rejects_an_empty_url():
    assert kiwix.split_content_path("") == (None, [])


def test_content_url_is_absolute(cfg):
    assert kiwix.content_url(cfg, "/content/wiki/Emu_War") == \
        "http://127.0.0.1:18080/content/wiki/Emu_War"


def test_fetch_article_falls_back_from_raw_to_content(cfg):
    session = FakeSession([FakeResponse(404), FakeResponse(200, "<p>text</p>")])
    assert kiwix.fetch_article(cfg, "/content/wiki/A", session=session) == "<p>text</p>"
    assert "/raw/wiki/content/A" in session.calls[0]["url"]
    assert "/content/wiki/A" in session.calls[1]["url"]


def test_fetch_article_returns_none_when_both_fail(cfg):
    session = FakeSession([FakeResponse(404), FakeResponse(500)])
    assert kiwix.fetch_article(cfg, "/content/wiki/A", session=session) is None


def test_is_up_is_false_on_connection_error(cfg):
    session = FakeSession([requests.exceptions.ConnectionError("nope")])
    assert kiwix.is_up(cfg, session=session) is False


def test_zim_files_skips_missing_and_disabled(cfg, tmp_path):
    from dataclasses import replace

    from infoforge.config import Zim
    cfg2 = replace(cfg, zims=(
        Zim(file="wikipedia_en_all_nopic.zim", name="present"),
        Zim(file="absent.zim", name="missing"),
        Zim(file="off.zim", name="disabled", enabled=False),
    ))
    assert kiwix.zim_files(cfg2) == ["wikipedia_en_all_nopic.zim"]


def test_run_args_are_fixed_argv_with_explicit_zim_paths(cfg):
    args = kiwix._run_args(cfg, ["a.zim", "b.zim"])
    assert args[0] == "run"
    assert "--user" in args and args[args.index("--user") + 1] == "root"
    assert f"{cfg.zim_dir}:/data:z" in args
    assert args[-2:] == ["/data/a.zim", "/data/b.zim"]
    assert all(isinstance(a, str) for a in args)


def test_run_args_publish_uses_the_configured_bind(cfg):
    args = kiwix._run_args(cfg, ["a.zim"])
    assert args[args.index("-p") + 1] == "127.0.0.1:18080:8080"


# ------------------------------------------------------- container lifecycle

class FakePodman:
    """Stands in for the podman CLI; records every argv it is handed."""

    def __init__(self, *, exists=True, state="running", started="", cmd=None):
        self.exists = exists
        self.state = state
        self.started = started
        self.cmd = cmd if cmd is not None else []
        self.argvs: list[tuple[str, ...]] = []

    def __call__(self, *args, check=False):
        import subprocess
        self.argvs.append(args)
        out = ""
        rc = 0
        if args[:2] == ("container", "exists"):
            rc = 0 if self.exists else 1
        elif args[:2] == ("inspect", "-f"):
            fmt = args[2]
            out = {"{{.State.Status}}": self.state,
                   "{{.State.StartedAt}}": self.started,
                   "{{json .Config.Cmd}}": __import__("json").dumps(self.cmd)}[fmt]
        elif args[0] == "run":
            self.exists, self.state = True, "running"
            self.cmd = ["-k", *[a for a in args if a.startswith("/data/")]]
        elif args[0] == "start":
            self.state = "running"
        elif args[0] == "stop":
            self.state = "exited"
        elif args[:2] == ("rm", "-f"):
            self.exists, self.state, self.cmd = False, "missing", []
        return subprocess.CompletedProcess(args, rc, out, "")

    @property
    def ran(self) -> bool:
        return any(a[0] == "run" for a in self.argvs)


def test_ensure_creates_a_missing_container(cfg, monkeypatch):
    fake = FakePodman(exists=False, state="missing")
    monkeypatch.setattr(kiwix, "_podman", fake)
    result = kiwix.ensure(cfg)
    assert result["action"] == "created"
    assert result["files"] == ["wikipedia_en_all_nopic.zim"]
    assert fake.ran


def test_ensure_leaves_a_healthy_container_alone(cfg, monkeypatch):
    fake = FakePodman(
        started="2999-01-01 00:00:00.123456 +0000 UTC",
        cmd=["-k", "/data/wikipedia_en_all_nopic.zim"],
    )
    monkeypatch.setattr(kiwix, "_podman", fake)
    assert kiwix.ensure(cfg)["action"] == "none"
    assert not fake.ran


def test_ensure_recreates_when_the_zim_set_changed(cfg, monkeypatch):
    # kiwix-serve reads its file list once at startup, so a container serving
    # the wrong set must be rebuilt, not restarted.
    fake = FakePodman(
        started="2999-01-01 00:00:00 +0000 UTC",
        cmd=["-k", "/data/some_other.zim"],
    )
    monkeypatch.setattr(kiwix, "_podman", fake)
    result = kiwix.ensure(cfg)
    assert result["action"] == "recreated"
    assert result["reason"] == "zim set changed"


def test_ensure_recreates_when_a_zim_is_newer_than_the_container(cfg, monkeypatch):
    fake = FakePodman(
        started="1999-01-01 00:00:00 +0000 UTC",
        cmd=["-k", "/data/wikipedia_en_all_nopic.zim"],
    )
    monkeypatch.setattr(kiwix, "_podman", fake)
    result = kiwix.ensure(cfg)
    assert result["action"] == "recreated"
    assert result["reason"] == "newer zim on disk"


def test_ensure_starts_a_stopped_container(cfg, monkeypatch):
    fake = FakePodman(
        state="exited",
        started="2999-01-01 00:00:00 +0000 UTC",
        cmd=["-k", "/data/wikipedia_en_all_nopic.zim"],
    )
    monkeypatch.setattr(kiwix, "_podman", fake)
    assert kiwix.ensure(cfg)["action"] == "started"
    assert ("start", kiwix.CONTAINER_NAME) in fake.argvs


def test_ensure_refuses_with_a_pointed_message_when_no_zims_exist(cfg, monkeypatch):
    from dataclasses import replace

    import pytest

    from infoforge.config import Zim
    monkeypatch.setattr(kiwix, "_podman", FakePodman())
    cfg2 = replace(cfg, zims=(Zim(file="absent.zim", name="missing"),))
    with pytest.raises(kiwix.KiwixError, match="no enabled .zim files"):
        kiwix.ensure(cfg2)


def test_stop_is_a_noop_when_missing(cfg, monkeypatch):
    fake = FakePodman(exists=False, state="missing")
    monkeypatch.setattr(kiwix, "_podman", fake)
    assert kiwix.stop(cfg) == {"action": "none", "state": "missing"}


def test_podman_started_epoch_parses_podman_timestamps(cfg, monkeypatch):
    # podman prints fractional seconds and a TZ *name*; neither is parseable
    # by strptime, so both must be stripped first.
    monkeypatch.setattr(
        kiwix, "_podman",
        FakePodman(started="2026-08-20 09:48:48.54468448 +0900 JST"),
    )
    assert kiwix._container_started_epoch() > 0


# ----------------------------------------------------------- 2.1 hardening

def test_book_segment_is_quoted_in_citation_and_fetch_urls(cfg):
    # A book name that decodes to "x?y/z" must stay ONE path segment.
    hostile = "/content/x%3Fy%2Fz/A"
    book, _ = kiwix.split_content_path(hostile)
    assert book == "x?y/z"
    assert kiwix.content_url(cfg, hostile) == "http://127.0.0.1:18080/content/x%3Fy%2Fz/A"
    session = FakeSession([FakeResponse(200, "<p>t</p>")])
    kiwix.fetch_article(cfg, hostile, session=session)
    url = session.calls[0]["url"]
    assert url == "http://127.0.0.1:18080/raw/x%3Fy%2Fz/content/A"
    assert "?" not in url


def test_quote_book_leaves_ordinary_names_alone():
    assert kiwix.quote_book("wikipedia_en_all_nopic") == "wikipedia_en_all_nopic"


def test_the_image_is_pinned_by_the_configured_tag(cfg):
    from dataclasses import replace
    assert kiwix.image_ref(cfg) == f"ghcr.io/kiwix/kiwix-serve:{cfg.kiwix_image_tag}"
    assert cfg.kiwix_image_tag != "latest"
    pinned = replace(cfg, kiwix_image_tag="3.7.0")
    args = kiwix._run_args(pinned, ["a.zim"])
    assert "ghcr.io/kiwix/kiwix-serve:3.7.0" in args
    assert "ghcr.io/kiwix/kiwix-serve" not in args  # never the bare, floating name


def test_missing_podman_is_a_catalogued_error(monkeypatch):
    import subprocess

    import pytest

    def boom(*a, **k):
        raise FileNotFoundError("podman")
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(kiwix.KiwixError) as exc:
        kiwix.container_state()
    assert exc.value.code == "podman_missing"
    assert "kiwix_url" in str(exc.value)


def test_podman_available_follows_path(monkeypatch):
    monkeypatch.setattr(kiwix.shutil, "which", lambda name: None)
    assert kiwix.podman_available() is False
    monkeypatch.setattr(kiwix.shutil, "which", lambda name: "/usr/bin/podman")
    assert kiwix.podman_available() is True


def test_no_enabled_zims_has_its_own_code(cfg, monkeypatch):
    from dataclasses import replace

    import pytest

    from infoforge.config import Zim
    monkeypatch.setattr(kiwix, "_podman", FakePodman())
    cfg2 = replace(cfg, zims=(Zim(file="absent.zim", name="missing"),))
    with pytest.raises(kiwix.KiwixError) as exc:
        kiwix.ensure(cfg2)
    assert exc.value.code == "kiwix_no_zims"
    with pytest.raises(kiwix.KiwixError) as exc:
        kiwix.recreate(cfg2)
    assert exc.value.code == "kiwix_no_zims"


def test_is_up_passes_its_timeout_through(cfg):
    seen = {}

    class S:
        def get(self, url, timeout=None, **kw):
            seen["timeout"] = timeout
            return FakeResponse(200)
    assert kiwix.is_up(cfg, session=S(), timeout=2) is True
    assert seen["timeout"] == 2
    assert kiwix.is_up(cfg, session=S()) is True
    assert seen["timeout"] == 5


def test_is_up_is_false_on_a_server_error(cfg):
    assert kiwix.is_up(cfg, session=FakeSession([FakeResponse(503)])) is False


# ------------------------------------- review fixes (opus): url root + licence

def test_a_kiwix_url_with_a_root_location_is_stripped_before_the_book(cfg):
    # kiwix-serve --urlRootLocation=/kiwix (e.g. behind a reverse proxy) returns
    # links like /kiwix/content/<book>/<path>; the root used to be read as the
    # book, so every fetch 404'd and retrieval found nothing.
    from dataclasses import replace
    rooted = replace(cfg, kiwix_url="http://127.0.0.1:18080/kiwix")
    link = "/kiwix/content/wikipedia_en/A/Emu_War"
    assert kiwix.split_content_path(link, kiwix.url_root(rooted)) == \
        ("wikipedia_en", ["A", "Emu_War"])
    assert kiwix.content_url(rooted, link) == \
        "http://127.0.0.1:18080/kiwix/content/wikipedia_en/A/Emu_War"
    session = FakeSession([FakeResponse(200, "<p>t</p>")])
    assert kiwix.fetch_article(rooted, link, session=session) == "<p>t</p>"
    assert session.calls[0]["url"] == \
        "http://127.0.0.1:18080/kiwix/raw/wikipedia_en/content/A/Emu_War"
    # No root configured: unchanged behaviour.
    assert kiwix.url_root(cfg) == ""
    assert kiwix.split_content_path("/content/w/A")[0] == "w"


def test_licence_lookup_matches_a_percent_encoded_book(cfg):
    from dataclasses import replace

    from infoforge.config import Zim
    named = replace(cfg, zims=(Zim(file="my notes+.zim", license="CC0"),))
    url = kiwix.content_url(named, "/content/my%20notes%2B/A/x")
    assert "/content/my%20notes%2B/" in url
    assert named.license_for_url(url) == "CC0"

