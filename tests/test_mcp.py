"""The MCP server: tool bodies directly, and the real server over an in-memory client."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from helpers import SOURCES

from infoforge import answer as answer_mod
from infoforge import kiwix, mcp_server, retrieve
from infoforge.answer import Answer
from infoforge.config import Endpoint


@pytest.fixture(autouse=True)
def _reset_config_path():
    yield
    mcp_server.set_config_path(None)


@pytest.fixture
def served(config_file, monkeypatch):
    monkeypatch.setattr(retrieve, "retrieve", lambda cfg, q, max_articles=None, session=None:
                        list(SOURCES))

    def fake(cfg, question, sources, session=None):
        return Answer(text="According to Emu War, 1932.",
                      endpoint=Endpoint("test-model", "http://x/v1"),
                      cited=["Emu War"], uncited=False, verdict="answered",
                      verdict_source="model")
    monkeypatch.setattr(answer_mod, "answer_question", fake)
    return config_file


# ------------------------------------------------------- tool bodies (no SDK)

def test_the_tool_set_is_exactly_the_read_only_five():
    assert mcp_server.TOOL_NAMES == ("search", "ask", "status", "doctor", "zim_list")
    assert tuple(mcp_server.TOOLS) == mcp_server.TOOL_NAMES
    forbidden = {"serve_start", "serve_stop", "serve_recreate", "zimify", "zim_check_update"}
    assert not forbidden & set(mcp_server.TOOLS)
    for fn in mcp_server.TOOLS.values():
        assert fn.__doc__ and len(fn.__doc__.strip()) > 20


def test_search_tool(served):
    out = mcp_server.tool_search("emu war")
    assert out["ok"] is True and out["query"] == "emu war"
    assert "text" not in out["sources"][0]
    with_text = mcp_server.tool_search("emu war", max_articles=1, include_text=True)
    assert with_text["sources"][0]["text"] == "1932 operation."


def test_search_tool_never_ensures(served, monkeypatch):
    monkeypatch.setattr(kiwix, "is_up", lambda cfg, session=None, timeout=5: False)
    monkeypatch.setattr(kiwix, "ensure", lambda cfg: pytest.fail("MCP must not create containers"))
    out = mcp_server.tool_search("emu war")
    assert out["ok"] is False and out["exit_code"] == 2
    assert out["error"]["code"] == "kiwix_unreachable" and out["sources"] == []


def test_search_tool_reports_no_sources_as_data(served, monkeypatch):
    monkeypatch.setattr(retrieve, "retrieve", lambda *a, **k: [])
    out = mcp_server.tool_search("zqxv")
    assert (out["ok"], out["reason"], out["error"]["code"]) == (False, "no_sources", "no_sources")


def test_ask_tool(served):
    out = mcp_server.tool_ask("what was the emu war")
    assert out["ok"] is True and out["answer"].startswith("According to")
    assert out["attribution"]
    assert out["sources"][0]["license"] == "unknown"  # test.zim is not Wikipedia-family


def test_ask_tool_never_ensures(served, monkeypatch):
    monkeypatch.setattr(kiwix, "is_up", lambda cfg, session=None, timeout=5: False)
    out = mcp_server.tool_ask("q")
    assert out["error"]["code"] == "kiwix_unreachable"


def test_status_zim_list_doctor_tools(served, monkeypatch):
    def no_podman(cfg):
        raise kiwix.KiwixError("x", code="podman_missing")
    monkeypatch.setattr(kiwix, "status", no_podman)
    status = mcp_server.tool_status()
    assert status["ok"] is True and status["state"] == "unavailable"
    zims = mcp_server.tool_zim_list()
    assert zims["ok"] is True and zims["zims"][0]["file"] == "test.zim"
    monkeypatch.setattr(kiwix, "podman_available", lambda: False)
    monkeypatch.setattr(kiwix, "search", lambda *a, **k: [object()])
    import requests

    class Ok:
        status_code = 200

        def json(self):
            return {"data": []}
    monkeypatch.setattr(requests, "get", lambda *a, **k: Ok())
    doctor = mcp_server.tool_doctor()
    assert {"ok", "checks"} <= set(doctor)


def test_config_errors_come_back_as_data(tmp_path):
    mcp_server.set_config_path(str(tmp_path / "absent.yaml"))
    for call in (lambda: mcp_server.tool_status(), lambda: mcp_server.tool_zim_list(),
                 lambda: mcp_server.tool_search("x")):
        out = call()
        assert out["ok"] is False and out["error"]["code"] == "config_missing"
    assert mcp_server.tool_ask("x")["error"]["code"] == "config_missing"
    assert mcp_server.tool_doctor()["ok"] is False


def test_main_list_tools_needs_no_sdk(capsys):
    assert mcp_server.main(["--list-tools"]) == 0
    assert capsys.readouterr().out.split() == list(mcp_server.TOOL_NAMES)


def test_main_runs_the_server_over_stdio(monkeypatch):
    ran = {}

    class Fake:
        def run(self, transport):
            ran["transport"] = transport
    monkeypatch.setattr(mcp_server, "build_server", lambda: Fake())
    assert mcp_server.main(["--config", "x.yaml"]) == 0
    assert ran == {"transport": "stdio"}
    assert mcp_server._config_path == "x.yaml"


def test_version_flag(capsys):
    with pytest.raises(SystemExit):
        mcp_server.main(["--version"])
    assert "infoforge-mcp" in capsys.readouterr().out


def test_build_server_passes_the_package_version_when_the_sdk_takes_one(monkeypatch):
    """2.x MCPServer accepts version= (reported in the initialize handshake); 1.x
    FastMCP does not, and build_server must fall back rather than fail."""
    seen = {}

    class Modern:
        def __init__(self, name, *, instructions, version):
            seen.update(name=name, instructions=instructions, version=version)

        def tool(self, **kwargs):
            return lambda fn: fn

    class Legacy:
        def __init__(self, name, *, instructions):
            seen.update(name=name, instructions=instructions, version="absent")

        def tool(self, **kwargs):
            return lambda fn: fn

    from infoforge import __version__
    monkeypatch.setattr(mcp_server, "_server_class", lambda: Modern)
    monkeypatch.setattr(mcp_server, "_read_only_annotations", lambda: None)
    mcp_server.build_server()
    assert seen == {"name": "infoforge", "instructions": mcp_server.INSTRUCTIONS,
                    "version": __version__}
    monkeypatch.setattr(mcp_server, "_server_class", lambda: Legacy)
    mcp_server.build_server()
    assert seen["version"] == "absent"


# ----------------------------------------- the real server, in-memory client

pytest.importorskip("mcp")


@contextlib.asynccontextmanager
async def connected():
    """An initialised client session against build_server(), for SDK 1.x and 2.x."""
    server = mcp_server.build_server()
    try:
        from mcp import Client
    except ImportError:
        Client = None  # noqa: N806
    if Client is not None:                      # mcp 2.x
        async with Client(server) as client:
            yield client
        return
    from mcp.shared.memory import create_connected_server_and_client_session
    async with create_connected_server_and_client_session(
            server._mcp_server) as session:      # mcp 1.x
        yield session


def pick(obj, camel, snake):
    """Attribute by its 1.x (camelCase) or 2.x (snake_case) name."""
    return getattr(obj, camel) if hasattr(obj, camel) else getattr(obj, snake)


def structured(result):
    """The tool's structured result (a typed dict return gives structured output)."""
    for name in ("structured_content", "structuredContent"):
        value = getattr(result, name, None)
        if value:
            return value
    raise AssertionError(f"no structured content in {result!r}")


def test_server_lists_exactly_the_five_read_only_tools(served):
    async def go():
        async with connected() as client:
            listed = await client.list_tools()
            return listed.tools

    tools = asyncio.run(go())
    assert [t.name for t in tools] == list(mcp_server.TOOL_NAMES)
    by_name = {t.name: t for t in tools}
    schema = pick(by_name["search"], "inputSchema", "input_schema")
    assert set(schema["properties"]) == {"query", "max_articles", "include_text"}
    assert schema["required"] == ["query"]
    assert pick(by_name["ask"], "inputSchema", "input_schema")["required"] == ["question"]
    for tool in tools:
        ann = tool.annotations
        assert ann is not None
        assert pick(ann, "readOnlyHint", "read_only_hint") is True
        assert pick(ann, "destructiveHint", "destructive_hint") is False


def test_server_search_and_ask_round_trip(served):
    async def go():
        async with connected() as client:
            found = await client.call_tool("search", {"query": "emu war",
                                                      "include_text": True})
            answered = await client.call_tool("ask", {"question": "what was the emu war"})
            listed = await client.call_tool("zim_list", {})
            return found, answered, listed

    found, answered, listed = asyncio.run(go())
    assert structured(found)["sources"][0]["text"] == "1932 operation."
    assert structured(answered)["answer"].startswith("According to")
    assert structured(listed)["zims"][0]["file"] == "test.zim"


def test_server_surfaces_catalogued_errors(served, monkeypatch):
    monkeypatch.setattr(kiwix, "is_up", lambda cfg, session=None, timeout=5: False)

    async def go():
        async with connected() as client:
            return await client.call_tool("search", {"query": "x"})

    out = structured(asyncio.run(go()))
    assert out["ok"] is False and out["error"]["code"] == "kiwix_unreachable"


# ------------------------------------------- review fix: bad max_articles (opus)

@pytest.mark.parametrize("bad", [0, -3, True])
def test_bad_max_articles_is_a_usage_error_not_no_sources(served, bad):
    # A negative count used to retrieve nothing and come back as no_sources,
    # which the server's instructions tell the model to report as a real
    # "the library does not cover this" answer.
    result = mcp_server.tool_search("emu war", max_articles=bad)
    assert result["ok"] is False
    assert result["error"]["code"] == "usage_error" and result["exit_code"] == 2
    asked = mcp_server.tool_ask("emu war", max_articles=bad)
    assert asked["ok"] is False and asked["error"]["code"] == "usage_error"


def test_good_max_articles_still_passes_through(served, monkeypatch):
    seen = []
    monkeypatch.setattr(retrieve, "retrieve", lambda cfg, q, max_articles=None, session=None:
                        seen.append(max_articles) or list(SOURCES))
    assert mcp_server.tool_search("emu war", max_articles=2)["ok"] is True
    assert mcp_server.tool_search("emu war")["ok"] is True
    assert seen == [2, None]
