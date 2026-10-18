"""InfoForge as a DisPatch add-on: the ``wiki_search`` live tool.

DisPatch discovers this module through the ``dispatch.extensions`` entry point
(``infoforge = "infoforge_dispatch:EXTENSION"``, see pyproject.toml), reads the
cheap ``EXTENSION`` metadata, checks ``api[0] <= EXT_API <= api[1]``, then calls
``setup(ctx)`` once.  Shape documented in DisPatch's ``docs/extensions.md``
("Live tools from a package").

This module must stay cheap and safe to import: it imports nothing from
DisPatch (the ``ctx`` is duck-typed) and defers every InfoForge import to call
time, so importing it on a machine without DisPatch, or without a config,
never fails.

The tool is read-only: it calls :func:`infoforge.api.search` with
``ensure=False`` (it never creates or recreates the kiwix container) and runs
the blocking HTTP work in a worker thread so the event loop is never stalled.
"""

from __future__ import annotations

import asyncio
from typing import Any

from infoforge import __version__

#: Handshake metadata (read before ``setup`` is imported).  ``setup`` must be a
#: ``"module:function"`` string.
EXTENSION: dict[str, Any] = {
    "id": "infoforge",
    "name": "InfoForge (offline Wikipedia)",
    "version": __version__,
    "api": [1, 1],
    "setup": "infoforge_dispatch:setup",
    "env": ["INFOFORGE_CONFIG"],
}

TOOL_NAME = "wiki_search"
DEFAULT_ARTICLES = 3
MAX_ARTICLES = 5
MAX_QUERY_CHARS = 500
#: Characters of article text returned in total (DisPatch cuts a tool result to
#: its own tool_result_chars, default 4000, so stay under that).
RESULT_CHARS = 3600
#: The turn waits for the tool, so time-box it; the underlying HTTP calls have
#: their own (longer) timeouts and are abandoned, not killed, after this.
TIMEOUT_S = 12.0

DESCRIPTION = (
    "Look something up in the offline Wikipedia copy on this machine. Returns the "
    "most relevant article extracts with their titles and URLs. Use it for factual "
    "questions (people, places, events, science, definitions) and cite the article "
    "title in your answer. If it reports no articles, say the library does not "
    "cover it rather than answering from memory."
)

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string",
                  "description": "What to look up, in plain words (a name, a topic, or "
                                 "a short question)."},
        "max_articles": {"type": "integer",
                         "description": f"How many articles to return, 1-{MAX_ARTICLES} "
                                        f"(default {DEFAULT_ARTICLES})."},
    },
    "required": ["query"],
}


def _clean_query(raw: Any) -> str:
    return " ".join(str(raw or "").split())[:MAX_QUERY_CHARS]


def _clean_count(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return DEFAULT_ARTICLES
    return max(1, min(MAX_ARTICLES, raw))


def _lookup(query: str, max_articles: int) -> str:
    """Blocking search -> the text the model reads."""
    from infoforge import api
    from infoforge.errors import InfoForgeError, spec_for

    try:
        cfg = api.load_cfg()
        result = api.search(query, max_articles=max_articles, text=True,
                            ensure=False, cfg=cfg)
    except InfoForgeError as exc:
        if exc.code == "no_sources":
            return f"No articles found for {query!r} in the offline library."
        # The catalog's generic fix, not the exception text: no paths or hosts.
        spec = spec_for(exc.code, exc.exit_code)
        return f"Offline Wikipedia is unavailable ({spec.code}). {spec.fix}"

    sources = result["sources"]
    budget = max(400, RESULT_CHARS // max(1, len(sources)))
    blocks = []
    for i, src in enumerate(sources, 1):
        text = src.get("text", "")
        if len(text) > budget:
            text = text[:budget].rsplit(" ", 1)[0] + " ..."
        blocks.append(f"[{i}] {src['title']} ({src['url']})\n{text}")
    credit = api.attribution_for(cfg, sources)
    return "\n\n".join(blocks) + (f"\n\nAttribution: {credit}" if credit else "")


async def wiki_search(args: dict) -> str:
    """The live-tool function: validate the model's arguments, search off-loop."""
    query = _clean_query((args or {}).get("query"))
    if not query:
        return "query must be a non-empty string, e.g. {\"query\": \"Emu War\"}"
    count = _clean_count((args or {}).get("max_articles", DEFAULT_ARTICLES))
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_lookup, query, count), timeout=TIMEOUT_S)
    except TimeoutError:
        return "The offline Wikipedia lookup timed out; try again or answer without it."


def _summary(args: dict) -> str:
    return f"Offline Wikipedia: {_clean_query((args or {}).get('query'))[:60]}"


def tool_spec() -> dict[str, Any]:
    """The dict ``ctx.register_live_tools`` takes (same keys as ``livetools.LiveTool``)."""
    return {
        "name": TOOL_NAME,
        "description": DESCRIPTION,
        "parameters": PARAMETERS,
        "fn": wiki_search,
        "summary": _summary,
    }


def setup(ctx: Any) -> None:
    """Called once by DisPatch; only registers (slow work does not belong here)."""
    ctx.register_live_tools([tool_spec()])
