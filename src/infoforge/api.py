"""The library API: every InfoForge operation as a function returning plain dicts.

The CLI (:mod:`infoforge.cli`) is a thin printer over these functions, and the
MCP server and the DisPatch add-on call them directly, so all three surfaces
see the same behaviour, the same JSON shapes and the same error catalog.

Conventions
-----------
* Blocking calls (``requests``).  From async code use ``asyncio.to_thread``.
* ``config`` is a path (or None for the default resolution); ``cfg`` is an
  already-loaded :class:`~infoforge.config.Config` (skips the file read).
* :func:`search`, :func:`status`, :func:`zim_list`, :func:`zim_check_update`,
  :func:`config_show` raise :class:`~infoforge.errors.InfoForgeError` on failure
  (``exc.code`` / ``exc.exit_code`` / ``errors.error_info(exc)``).
* :func:`ask` never raises an InfoForgeError: every outcome, including exit
  3/5/6, is a result envelope with ``ok``, ``exit_code``, ``reason``, ``sources``
  and, on failure, ``error: {code, message, fix, docs}`` — exit 5 still carries
  the sources that were found.
* :func:`search` and :func:`ask` only create or recreate the kiwix container
  when called with ``ensure=True``.  The default for ``search`` is ``False``:
  a caller on a deadline (DisPatch gives it 8 s) must never wait on podman.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable

import requests

from . import answer as answer_mod
from . import config as config_mod
from . import doctor as doctor_mod
from . import kiwix, retrieve, settings_schema
from .config import Config
from .errors import (
    EXIT_CITATION,
    EXIT_NOT_IN_SOURCES,
    EXIT_OK,
    REASON_BY_EXIT,
    REASON_OTHER,
    InfoForgeError,
    NoGroundingError,
    error_info,
    info_for_code,
)
from .logging_ import event, note, question_fields

CATALOG_URL = "https://library.kiwix.org/catalog/v2/entries"

#: How long `--no-ensure` waits for a kiwix-serve that accepts and then hangs.
#: A dead one (connection refused) answers instantly.
NO_ENSURE_PROBE_S = 2.0

CC_BY_SA_URL = "https://creativecommons.org/licenses/by-sa/4.0/"

Notify = Callable[[str], None] | None


# ------------------------------------------------------------------- helpers

def load_cfg(config: str | None = None, cfg: Config | None = None) -> Config:
    """``cfg`` if given, else the config at ``config`` (default resolution)."""
    return cfg if cfg is not None else config_mod.load(config)


def check_max_articles(max_articles: int | None) -> None:
    """Reject a ``max_articles`` override below 1 (None means "use the config").

    Without this a 0 silently meant "the config value" and a negative number
    retrieved nothing, which surfaced as ``no_sources`` -- a caller (or an MCP
    client told to treat ``no_sources`` as a real answer) would report that the
    library lacks the topic when the request itself was wrong.
    """
    if max_articles is None:
        return
    if isinstance(max_articles, bool) or not isinstance(max_articles, int) or max_articles < 1:
        raise InfoForgeError(f"max_articles must be an integer >= 1, got {max_articles!r}",
                             code="usage_error")


def ensure_kiwix(cfg: Config, session: requests.Session | None = None,
                 notify: Notify = None) -> None:
    """Make sure kiwix-serve answers, starting the container only if needed.

    Checking HTTP first means a kiwix started by systemd, by hand, or by
    another tool (or one you run on another machine) is left alone — podman is
    only touched when nothing responds.
    """
    if kiwix.is_up(cfg, session=session):
        return
    if not kiwix.podman_available():
        raise kiwix.KiwixError(
            f"nothing answers at {cfg.kiwix_url} and podman is not installed to start "
            "kiwix-serve",
            code="kiwix_unreachable",
        )
    result = kiwix.ensure(cfg)
    if result["action"] != "none":
        (notify or note)(
            f"kiwix container {result['action']}: serving {', '.join(result['files'])}")
    # kiwix-serve needs a moment to bind after a create/start.
    for _ in range(20):
        if kiwix.is_up(cfg, session=session):
            return
        time.sleep(0.5)
    raise kiwix.KiwixError(
        f"kiwix container is {result['state']} but nothing answers at {cfg.kiwix_url}\n"
        "  Check `infoforge serve status` and `podman logs infoforge-kiwix`.",
        code="kiwix_unreachable",
    )


def require_kiwix(cfg: Config, session: requests.Session | None = None) -> None:
    """Fail fast (no podman, no waiting) when kiwix-serve does not answer."""
    if not kiwix.is_up(cfg, session=session, timeout=NO_ENSURE_PROBE_S):
        raise kiwix.KiwixError(
            f"kiwix-serve does not answer at {cfg.kiwix_url}\n"
            "  Start it with `infoforge serve start`, or point kiwix_url at a running "
            "kiwix-serve.",
            code="kiwix_unreachable",
        )


def _licensed(cfg: Config, sources: list[retrieve.Source]) -> list[tuple[retrieve.Source, str]]:
    return [(s, cfg.license_for_url(s.url)) for s in sources]


def source_rows(cfg: Config, sources: list[retrieve.Source]) -> list[dict]:
    """The ``sources`` rows of an ``ask`` envelope (title, url, score, license)."""
    return [{"title": s.title, "url": s.url, "score": s.score, "license": lic}
            for s, lic in _licensed(cfg, sources)]


def attribution(cfg: Config, sources: list[retrieve.Source]) -> str:
    """The attribution line for the sources an answer was built from."""
    return attribution_for(cfg, [{"title": s.title, "url": s.url} for s in sources])


def attribution_for(cfg: Config, rows: list[dict]) -> str:
    """Attribution for ``rows`` (dicts with ``title`` and ``url``).

    Wikipedia-family text is CC BY-SA 4.0, which requires credit and a link to
    the licence; the per-article contributor list is each article's history
    page on the project it came from.  A ZIM with no known licence is reported
    as such rather than silently attributed.
    """
    by_license: dict[str, list[str]] = {}
    for row in rows:
        by_license.setdefault(cfg.license_for_url(row["url"]), []).append(row["title"])
    parts = []
    for lic, titles in by_license.items():
        names = ", ".join(titles)
        if lic == config_mod.LICENSE_UNKNOWN:
            parts.append(f"{names}: licence unknown (check the ZIM's metadata before reuse).")
        elif lic.upper().startswith("CC BY-SA"):
            parts.append(f"{names}: from Wikipedia and sister projects, licensed {lic} "
                         f"({CC_BY_SA_URL}); contributors are credited in each article's "
                         "page history.")
        else:
            parts.append(f"{names}: licensed {lic}.")
    return " ".join(parts)


# -------------------------------------------------------------------- search

def search(question: str, *, max_articles: int | None = None, text: bool = False,
           ensure: bool = False, config: str | None = None, cfg: Config | None = None,
           session: requests.Session | None = None) -> dict:
    """Retrieval only.  Returns ``{"query": ..., "sources": [{title, url, score, chars[, text]}]}``.

    This is exactly the document ``infoforge search --json [--text]`` prints.
    Raises :class:`~infoforge.errors.InfoForgeError` (``no_sources`` -> exit 3,
    ``kiwix_unreachable`` -> exit 2, ...).  With ``ensure=False`` (the default
    here) a dead kiwix is a fast error, never a container start.
    """
    cfg = load_cfg(config, cfg)
    question = question.strip()
    if not question:
        raise InfoForgeError('search needs a query: infoforge search "..."',
                             code="usage_error")
    check_max_articles(max_articles)
    session = session or requests.Session()
    if ensure:
        ensure_kiwix(cfg, session=session)
    else:
        require_kiwix(cfg, session=session)
    sources = retrieve.retrieve(cfg, question, max_articles=max_articles, session=session)
    if not sources:
        raise NoGroundingError(f"no sources found for {question!r}")
    event("search", **question_fields(question, cfg.log_questions), sources=len(sources))

    rows = []
    for s in sources:
        row = {"title": s.title, "url": s.url, "score": s.score, "chars": len(s.text)}
        if text:
            # The extracted article text itself (already cut to max_chars
            # around the question's keywords), for a caller that answers with
            # its own model instead of `ask`.
            row["text"] = s.text
        rows.append(row)
    return {"query": question, "sources": rows}


# ----------------------------------------------------------------------- ask

def _envelope(cfg: Config | None, exit_code: int, question: str,
              sources: list[retrieve.Source], error: dict | None = None, **extra) -> dict:
    out: dict = {
        "ok": exit_code == EXIT_OK,
        "exit_code": exit_code,
        "reason": REASON_BY_EXIT.get(exit_code, REASON_OTHER),
        "question": question,
        **extra,
    }
    if error is not None:
        out["error"] = error
    if cfg is not None:
        out["sources"] = source_rows(cfg, sources)
        if sources:
            out["attribution"] = attribution(cfg, sources)
    else:
        out["sources"] = [{"title": s.title, "url": s.url, "score": s.score} for s in sources]
    return out


def ask(question: str, *, strict: bool = False, max_articles: int | None = None,
        ensure: bool = True, config: str | None = None, cfg: Config | None = None,
        session: requests.Session | None = None, notify: Notify = None) -> dict:
    """The whole pipeline.  Always returns the result envelope (see module doc)."""
    question = question.strip()
    sources: list[retrieve.Source] = []
    loaded: Config | None = cfg
    try:
        loaded = load_cfg(config, cfg)
        return _ask(loaded, question, sources, strict=strict, max_articles=max_articles,
                    ensure=ensure, session=session, notify=notify)
    except InfoForgeError as exc:
        return _envelope(loaded, exc.exit_code, question, sources, error=error_info(exc))


def _ask(cfg: Config, question: str, sources: list[retrieve.Source], *, strict: bool,
         max_articles: int | None, ensure: bool, session: requests.Session | None,
         notify: Notify) -> dict:
    if not question:
        raise InfoForgeError('ask needs a question: infoforge ask "..."', code="usage_error")
    check_max_articles(max_articles)

    # One session for the whole run: retrieval makes a dozen-plus HTTP calls
    # to the same two hosts, and per-call connections add up.
    session = session or requests.Session()
    if ensure:
        ensure_kiwix(cfg, session=session, notify=notify)
    else:
        require_kiwix(cfg, session=session)
    sources.extend(retrieve.retrieve(cfg, question, max_articles=max_articles,
                                     session=session))
    if not sources:
        raise NoGroundingError(
            f"no sources found for {question!r}\n"
            f"  Tried {len(cfg.enabled_zims)} enabled ZIM(s) via {cfg.kiwix_url}.\n"
            "  Try fewer/plainer keywords, or check `infoforge doctor`."
        )
    qfields = question_fields(question, cfg.log_questions)
    titles = {"titles": [s.title for s in sources]} if cfg.log_questions else {}
    event("retrieve", **qfields, sources=len(sources), **titles)
    if notify:
        notify(f"grounding on {len(sources)} article(s)")

    # Exit 5 (every LLM down) propagates as LLMUnreachableError; `ask` turns it
    # into an envelope that still lists the sources, so the run is not a total
    # loss.
    result = answer_mod.answer_question(cfg, question, sources, session=session)

    # Order matters: a NOT_IN_SOURCES reply usually names a title ("Eiffel
    # Tower does not mention..."), so it passes the citation check — the
    # verdict must be decided first, under strict or not.
    if result.not_in_sources:
        code = EXIT_NOT_IN_SOURCES
    elif result.uncited and strict:
        code = EXIT_CITATION
    else:
        code = EXIT_OK

    # Titles reveal the topic as surely as the question does: counts only,
    # unless the owner opted in to question logging.
    cited = {"cited": result.cited} if cfg.log_questions else {"cited_n": len(result.cited)}
    event("ask", **qfields, model_id=result.endpoint.model_id, **cited,
          uncited=result.uncited, verdict=result.verdict,
          verdict_source=result.verdict_source, exit_code=code)

    error = None
    if code == EXIT_NOT_IN_SOURCES:
        error = info_for_code("not_in_sources")
    elif code == EXIT_CITATION:
        error = info_for_code("uncited")
    return _envelope(
        cfg, code, question, sources, error=error,
        answer=result.text,
        verdict=result.verdict,
        verdict_source=result.verdict_source,
        model_id=result.endpoint.model_id,
        cited=result.cited,
        uncited=result.uncited,
    )


# -------------------------------------------------------------------- status

def status(*, config: str | None = None, cfg: Config | None = None,
           session: requests.Session | None = None) -> dict:
    """Container state, what it serves, and whether kiwix answers.

    Works without podman (``state: "unavailable"``): a bring-your-own
    kiwix-serve install has no container to report on.
    """
    cfg = load_cfg(config, cfg)
    try:
        if not kiwix.podman_available():
            raise kiwix.KiwixError("podman is not installed", code="podman_missing")
        info = kiwix.status(cfg)
        info["podman"] = True
    except kiwix.KiwixError as exc:
        if exc.code != "podman_missing":
            raise
        info = {
            "name": kiwix.CONTAINER_NAME,
            "state": "unavailable",
            "kiwix_url": cfg.kiwix_url,
            "configured": kiwix.zim_files(cfg) if cfg.zim_dir.is_dir() else [],
            "serving": [],
            "podman": False,
        }
    info["kiwix_responding"] = kiwix.is_up(cfg, session=session)
    return info


# -------------------------------------------------------------------- doctor

def doctor(*, config: str | None = None,
           session: requests.Session | None = None) -> dict:
    """Run every health check; returns ``{"ok": bool, "checks": [...]}`` (never raises)."""
    report = doctor_mod.run(config, session=session)
    event("doctor", ok=report.ok)
    return report.to_dict()


# ----------------------------------------------------------------------- zim

def zim_list(*, config: str | None = None, cfg: Config | None = None) -> dict:
    """The configured ZIMs, whether each exists on disk, and any unregistered files."""
    cfg = load_cfg(config, cfg)
    on_disk = set(kiwix.zim_dir_listing(cfg.zim_dir))
    rows = []
    for zim in cfg.zims:
        rows.append({"file": zim.file, "name": zim.name, "enabled": zim.enabled,
                     "present": zim.file in on_disk})
    for name in sorted(on_disk - {z.file for z in cfg.zims}):
        rows.append({"file": name, "name": "(not in config)", "enabled": False,
                     "present": True})
    return {"zim_dir": str(cfg.zim_dir), "zims": rows}


_DATE_RE = re.compile(r"(\d{4})-(\d{2})")


def zim_check_update(*, config: str | None = None, cfg: Config | None = None) -> dict:
    """Ask library.kiwix.org whether a newer dump exists.  REPORT ONLY.

    This never downloads anything: ZIM dumps are tens of gigabytes and that is
    a deliberate human decision, not something a status command should start.
    It is the one InfoForge call that leaves the machine (to the Kiwix catalog).
    """
    cfg = load_cfg(config, cfg)
    results = []
    for zim in cfg.zims:
        stem = zim.books_name
        # Strip a trailing YYYY-MM so the catalog query matches the dump family.
        family = re.sub(r"_\d{4}-\d{2}$", "", stem)
        local = _DATE_RE.search(stem)
        entry = {"file": zim.file, "family": family,
                 "local_date": local.group(0) if local else None,
                 "latest": None, "newer_available": None, "error": None}
        try:
            resp = requests.get(CATALOG_URL,
                                params={"q": family, "count": "20"}, timeout=20)
            resp.raise_for_status()
            entry["latest"] = latest_catalog_date(resp.text, family)
        except requests.exceptions.RequestException as exc:
            entry["error"] = f"catalog unreachable: {exc}"
        except ET.ParseError as exc:
            entry["error"] = f"catalog response was not XML: {exc}"
        if entry["latest"] and entry["local_date"]:
            entry["newer_available"] = entry["latest"] > entry["local_date"]
        results.append(entry)
    return {"checked": results}


def latest_catalog_date(xml_text: str, family: str) -> str | None:
    """Newest YYYY-MM seen in catalog entry names matching ``family``."""
    root = ET.fromstring(xml_text)
    best = None
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "name":
            continue
        name = (element.text or "").strip()
        if family not in name:
            continue
        match = _DATE_RE.search(name)
        if match and (best is None or match.group(0) > best):
            best = match.group(0)
    return best


# -------------------------------------------------------------------- config

def config_show(*, config: str | None = None, cfg: Config | None = None) -> dict:
    """The effective configuration, each value with its source; secrets redacted."""
    return config_mod.show(load_cfg(config, cfg))


def config_schema() -> dict:
    """The JSON Schema for the config file (also what `config schema --json` prints)."""
    return settings_schema.json_schema()


__all__ = [
    "ask", "attribution", "attribution_for", "config_schema", "config_show", "doctor", "ensure_kiwix",
    "require_kiwix", "search", "status", "zim_check_update", "zim_list",
]
