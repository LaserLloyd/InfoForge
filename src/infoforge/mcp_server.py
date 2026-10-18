"""InfoForge as an MCP server (stdio): ``infoforge-mcp``.

Read-only tools over the same library API the CLI uses (:mod:`infoforge.api`):

==========  ================================================================
search      Retrieval only: ranked articles (and their text) from the ZIMs.
ask         The full grounded pipeline: answer + citations + attribution.
status      Container state and whether kiwix-serve answers.
doctor      Health checks for every link in the chain.
zim_list    The configured ZIMs and whether each exists on disk.
==========  ================================================================

Deliberately absent: ``serve_start/stop/recreate`` and ``zimify`` (they change
container state or write files) and ``zim_check_update`` (the one call that
leaves the machine).  Use the CLI for those.

These tools never create or recreate the kiwix container: a dead kiwix-serve is
a fast ``kiwix_unreachable`` error (``ensure=False``).  Failures are returned
as ``{"ok": false, "error": {code, message, fix, docs}}``, the same catalog
the CLI uses, never as a traceback.

Install with ``pip install 'infoforge[mcp]'`` (``mcp>=1.23,<3``; both the 1.x
``FastMCP`` and the 2.x ``MCPServer`` class are supported).  stdout is the
protocol channel, so nothing in here prints to it; notes go to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from typing import Any

from . import __version__, api
from .errors import REASON_BY_EXIT, REASON_OTHER, InfoForgeError, error_info

SERVER_NAME = "infoforge"

INSTRUCTIONS = (
    "Grounded lookups over an offline Kiwix library (Wikipedia and other ZIM archives). "
    "Use `search` to get source articles and answer them yourself; use `ask` only when "
    "you want InfoForge's own configured LLM to answer. A result with ok=false and "
    "reason 'no_sources' or 'not_in_sources' is a real answer: report it, do not guess."
)

#: Stable tool names, in registration order (also the contract diffed in CI).
TOOL_NAMES = ("search", "ask", "status", "doctor", "zim_list")

_ConfigPath = str | None
_config_path: _ConfigPath = None


def set_config_path(path: _ConfigPath) -> None:
    """Point every tool at a specific config file (default: normal resolution)."""
    global _config_path
    _config_path = path


def _fail(exc: InfoForgeError, **extra) -> dict:
    return {
        "ok": False,
        "exit_code": exc.exit_code,
        "reason": REASON_BY_EXIT.get(exc.exit_code, REASON_OTHER),
        "error": error_info(exc),
        **extra,
    }


# --------------------------------------------------- tool bodies (blocking, plain)

def tool_search(query: str, max_articles: int | None = None,
                include_text: bool = False) -> dict:
    """Search the offline library; returns ranked articles (title, url, score, chars).

    Set include_text to get each article's extracted text (cut around the query's
    keywords) so you can answer from it yourself. Never starts the kiwix container.
    """
    try:
        result = api.search(query, max_articles=max_articles, text=include_text,
                            ensure=False, config=_config_path)
    except InfoForgeError as exc:
        return _fail(exc, query=query, sources=[])
    return {"ok": True, **result}


def tool_ask(question: str, strict: bool = False,
             max_articles: int | None = None) -> dict:
    """Answer a question using ONLY retrieved articles, with citations and attribution.

    Calls the LLM configured for InfoForge. Check ok/reason: 'no_sources' (3) and
    'not_in_sources' (6) mean the library does not contain the answer; do not
    answer from memory instead. Never starts the kiwix container.
    """
    return api.ask(question, strict=strict, max_articles=max_articles, ensure=False,
                   config=_config_path)


def tool_status() -> dict:
    """Kiwix container state, what it serves, and whether kiwix-serve answers."""
    try:
        return {"ok": True, **api.status(config=_config_path)}
    except InfoForgeError as exc:
        return _fail(exc)


def tool_doctor() -> dict:
    """Run InfoForge's health checks (config, ZIMs, kiwix, search, LLM endpoints)."""
    return api.doctor(config=_config_path)


def tool_zim_list() -> dict:
    """List configured ZIM archives and whether each exists on disk."""
    try:
        return {"ok": True, **api.zim_list(config=_config_path)}
    except InfoForgeError as exc:
        return _fail(exc)


#: name -> blocking function.  The single list the server and the docs use.
TOOLS: dict[str, Callable[..., dict]] = {
    "search": tool_search,
    "ask": tool_ask,
    "status": tool_status,
    "doctor": tool_doctor,
    "zim_list": tool_zim_list,
}
assert tuple(TOOLS) == TOOL_NAMES


# ------------------------------------------------------------------- the server

def _server_class():
    """The SDK's server class: 2.x ``MCPServer``, else 1.x ``FastMCP``."""
    try:
        from mcp.server.mcpserver import MCPServer
        return MCPServer
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP
        return FastMCP
    except ImportError:
        raise SystemExit(
            "infoforge-mcp needs the MCP SDK: pip install 'infoforge[mcp]'"
        ) from None


def _read_only_annotations():
    try:
        from mcp.types import ToolAnnotations
    except ImportError:  # pragma: no cover - SDK layout changed
        return None
    return ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                           idempotentHint=True, openWorldHint=False)


def build_server():
    """Create the server with the five read-only tools registered."""
    cls = _server_class()
    try:
        # 2.x MCPServer reports `version` in the initialize handshake; 1.x
        # FastMCP has no such parameter.
        server = cls(SERVER_NAME, instructions=INSTRUCTIONS, version=__version__)
    except TypeError:
        try:
            server = cls(SERVER_NAME, instructions=INSTRUCTIONS)
        except TypeError:  # pragma: no cover - older signature
            server = cls(SERVER_NAME)
    annotations = _read_only_annotations()

    def register(fn):
        kwargs = {"annotations": annotations} if annotations is not None else {}
        server.tool(name=fn.__name__, description=TOOLS[fn.__name__].__doc__, **kwargs)(fn)
        return fn

    # Async wrappers: each blocking call runs in a worker thread so one slow
    # lookup never stalls the protocol loop.  Signatures are the schemas.
    @register
    async def search(query: str, max_articles: int | None = None,
                     include_text: bool = False) -> dict[str, Any]:
        return await asyncio.to_thread(tool_search, query, max_articles, include_text)

    @register
    async def ask(question: str, strict: bool = False,
                  max_articles: int | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(tool_ask, question, strict, max_articles)

    @register
    async def status() -> dict[str, Any]:
        return await asyncio.to_thread(tool_status)

    @register
    async def doctor() -> dict[str, Any]:
        return await asyncio.to_thread(tool_doctor)

    @register
    async def zim_list() -> dict[str, Any]:
        return await asyncio.to_thread(tool_zim_list)

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="infoforge-mcp",
        description="InfoForge MCP server (stdio). Read-only tools: " + ", ".join(TOOL_NAMES),
    )
    parser.add_argument("--version", action="version", version=f"infoforge-mcp {__version__}")
    parser.add_argument("--config", metavar="PATH",
                        help="config file (default: same resolution as the CLI)")
    parser.add_argument("--list-tools", action="store_true",
                        help="print the tool names and exit (does not need the MCP SDK)")
    args = parser.parse_args(argv)
    if args.list_tools:
        for name in TOOL_NAMES:
            print(name)
        return 0
    set_config_path(args.config)
    build_server().run("stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
