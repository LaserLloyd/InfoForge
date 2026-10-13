"""Command line interface.

Exit codes are a contract other tools depend on (see config.py):
  0 ok · 2 config/usage · 3 nothing to ground on · 4 --strict citation failure
  5 every LLM unreachable · 6 the model said the sources lack the answer.

`ask --json` prints one JSON object on every exit except a crash, always with
``ok``, ``exit_code``, ``reason`` and ``sources`` — a wrapper never has to parse
prose or cope with an empty stdout.

Note what exit 5 does *not* do: it still prints the sources that were found.
Retrieval and answering are separate halves, and a working search is useful on
its own when the brain is down.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from . import __version__, kiwix, retrieve
from . import answer as answer_mod
from . import doctor as doctor_mod
from .config import (
    EXIT_CITATION,
    EXIT_CONFIG,
    EXIT_LLM,
    EXIT_NO_GROUNDING,
    EXIT_NOT_IN_SOURCES,
    EXIT_OK,
    Config,
    InfoForgeError,
    NoGroundingError,
)
from .config import load as load_config
from .logging_ import event, note
from .zimify import zimify

CATALOG_URL = "https://library.kiwix.org/catalog/v2/entries"


# ------------------------------------------------------------------- helpers

def _ensure_kiwix(cfg: Config) -> None:
    """Make sure kiwix-serve answers, starting the container only if needed.

    Checking HTTP first means a kiwix started by systemd, by hand, or by
    another tool is left alone — podman is only touched when nothing responds.
    """
    if kiwix.is_up(cfg):
        return
    result = kiwix.ensure(cfg)
    if result["action"] != "none":
        note(f"kiwix container {result['action']}: serving {', '.join(result['files'])}")
    # kiwix-serve needs a moment to bind after a create/start.
    for _ in range(20):
        if kiwix.is_up(cfg):
            return
        time.sleep(0.5)
    raise InfoForgeError(
        f"kiwix container is {result['state']} but nothing answers at {cfg.kiwix_url}\n"
        "  Check `infoforge serve status` and `podman logs infoforge-kiwix`."
    )


def _print_sources(sources: list[retrieve.Source], stream=None) -> None:
    # Resolved at call time, not import time, so a redirected stdout is honoured.
    stream = stream or sys.stdout
    print("Citations:", file=stream)
    for i, src in enumerate(sources, 1):
        print(f"[{i}] {src.title} — {src.url}", file=stream)


# ---------------------------------------------------------------------- ask

# ``reason`` in `ask --json`, one per exit code (stable strings).
REASONS = {
    EXIT_OK: "answered",
    EXIT_CONFIG: "config",
    EXIT_NO_GROUNDING: "no_sources",
    EXIT_CITATION: "uncited",
    EXIT_LLM: "llm_unreachable",
    EXIT_NOT_IN_SOURCES: "not_in_sources",
}


def _source_rows(sources: list[retrieve.Source]) -> list[dict]:
    return [{"title": s.title, "url": s.url, "score": s.score} for s in sources]


def _emit_ask_json(exit_code: int, question: str, sources, **extra) -> None:
    print(json.dumps({
        "ok": exit_code == EXIT_OK,
        "exit_code": exit_code,
        "reason": REASONS.get(exit_code, "error"),
        "question": question,
        **extra,
        "sources": _source_rows(sources or []),
    }, indent=2))


def cmd_ask(args) -> int:
    question = " ".join(args.question).strip()
    sources: list[retrieve.Source] = []
    try:
        return _ask(args, question, sources)
    except InfoForgeError as exc:
        # Every failure still speaks JSON when asked to (exit 3 used to print
        # nothing at all on stdout).  Exit 5 also lists what retrieval found.
        if args.json:
            _emit_ask_json(exc.exit_code, question, sources, error=str(exc))
        elif sources:
            _print_sources(sources, stream=sys.stdout)
        raise


def _ask(args, question: str, sources: list[retrieve.Source]) -> int:
    """The ask pipeline.  ``sources`` is filled in place so cmd_ask can report
    them on a failure after retrieval."""
    cfg = load_config(args.config)
    if not question:
        raise InfoForgeError("ask needs a question: infoforge ask \"...\"")

    _ensure_kiwix(cfg)
    # One session for the whole run: retrieval makes a dozen-plus HTTP calls
    # to the same two hosts, and per-call connections add up.
    session = requests.Session()
    sources.extend(retrieve.retrieve(cfg, question, max_articles=args.max_articles,
                                     session=session))
    if not sources:
        raise NoGroundingError(
            f"no sources found for {question!r}\n"
            f"  Tried {len(cfg.enabled_zims)} enabled ZIM(s) via {cfg.kiwix_url}.\n"
            "  Try fewer/plainer keywords, or check `infoforge doctor`."
        )
    event("retrieve", question=question, sources=len(sources),
          titles=[s.title for s in sources])

    if not args.json:
        note(f"grounding on {len(sources)} article(s)")

    # Exit 5 (every LLM down) propagates to cmd_ask, which still prints the
    # sources, so the run is not a total loss.
    result = answer_mod.answer_question(cfg, question, sources, session=session)

    # Order matters: a NOT_IN_SOURCES reply usually names a title ("Eiffel
    # Tower does not mention…"), so it passes the citation check — the
    # verdict must be decided first, under --strict or not.
    if result.not_in_sources:
        code = EXIT_NOT_IN_SOURCES
    elif result.uncited and args.strict:
        code = EXIT_CITATION
    else:
        code = EXIT_OK

    event("ask", question=question, model_id=result.endpoint.model_id,
          cited=result.cited, uncited=result.uncited, verdict=result.verdict,
          verdict_source=result.verdict_source, exit_code=code)

    if args.json:
        _emit_ask_json(
            code, question, sources,
            answer=result.text,
            verdict=result.verdict,
            verdict_source=result.verdict_source,
            model_id=result.endpoint.model_id,
            cited=result.cited,
            uncited=result.uncited,
        )
    else:
        print(result.text)
        print()
        if result.uncited and not result.not_in_sources:
            print(answer_mod.UNCITED_WARNING)
            print()
        _print_sources(sources)

    if code == EXIT_NOT_IN_SOURCES:
        print("infoforge: not in the sources: the retrieved articles do not "
              "contain the answer (nothing was invented)", file=sys.stderr)
    elif code == EXIT_CITATION:
        print("infoforge: strict mode: answer cited no sources", file=sys.stderr)
    return code


# -------------------------------------------------------------------- search

def cmd_search(args) -> int:
    cfg = load_config(args.config)
    question = " ".join(args.question).strip()
    if not question:
        raise InfoForgeError("search needs a query: infoforge search \"...\"")

    _ensure_kiwix(cfg)
    sources = retrieve.retrieve(cfg, question, max_articles=args.max_articles,
                                session=requests.Session())
    if not sources:
        raise NoGroundingError(f"no sources found for {question!r}")
    event("search", question=question, sources=len(sources))

    if args.json:
        rows = []
        for s in sources:
            row = {"title": s.title, "url": s.url, "score": s.score,
                   "chars": len(s.text)}
            if args.text:
                # The extracted article text itself (already cut to
                # max_chars around the question's keywords), for a caller
                # that answers with its own model instead of `ask`.
                row["text"] = s.text
            rows.append(row)
        print(json.dumps({"query": question, "sources": rows}, indent=2))
    else:
        for i, src in enumerate(sources, 1):
            print(f"[{i}] {src.title}  (score {src.score:.1f}, {len(src.text)} chars)")
            print(f"    {src.url}")
    return EXIT_OK


# --------------------------------------------------------------------- serve

def cmd_serve(args) -> int:
    cfg = load_config(args.config)
    action = args.action
    if action == "start":
        result = kiwix.ensure(cfg)
        print(f"container {kiwix.CONTAINER_NAME}: {result['state']} ({result['action']})")
        print(f"  serving: {', '.join(result['files'])}")
        print(f"  url: {cfg.kiwix_url}")
    elif action == "stop":
        result = kiwix.stop(cfg)
        print(f"container {kiwix.CONTAINER_NAME}: {result['state']} ({result['action']})")
    elif action == "recreate":
        files = kiwix.recreate(cfg)
        print(f"container {kiwix.CONTAINER_NAME}: recreated")
        print(f"  serving: {', '.join(files)}")
    else:  # status
        return _print_status(cfg, args.json)
    event("serve", action=action)
    return EXIT_OK


def _print_status(cfg: Config, as_json: bool = False) -> int:
    info = kiwix.status(cfg)
    info["kiwix_responding"] = kiwix.is_up(cfg)
    if as_json:
        print(json.dumps(info, indent=2))
        return EXIT_OK
    print(f"container {info['name']}: {info['state']}")
    if info.get("ps"):
        print(f"  {info['ps']}")
    print(f"  serving:    {', '.join(info['serving']) or '(none)'}")
    print(f"  configured: {', '.join(info['configured']) or '(none)'}")
    print(f"  url:        {info['kiwix_url']} "
          f"({'responding' if info['kiwix_responding'] else 'no response'})")
    return EXIT_OK


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    return _print_status(cfg, args.json)


# -------------------------------------------------------------------- doctor

def cmd_doctor(args) -> int:
    report = doctor_mod.run(args.config)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.render())
    event("doctor", ok=report.ok)
    return EXIT_OK if report.ok else 1


# ----------------------------------------------------------------------- zim

_DATE_RE = re.compile(r"(\d{4})-(\d{2})")


def cmd_zim(args) -> int:
    cfg = load_config(args.config)
    if args.action == "list":
        return _zim_list(cfg, args.json)
    return _zim_check_update(cfg, args.json)


def _zim_list(cfg: Config, as_json: bool) -> int:
    on_disk = set(kiwix.zim_dir_listing(cfg.zim_dir))
    rows = []
    for zim in cfg.zims:
        rows.append({"file": zim.file, "name": zim.name, "enabled": zim.enabled,
                     "present": zim.file in on_disk})
    for name in sorted(on_disk - {z.file for z in cfg.zims}):
        rows.append({"file": name, "name": "(not in config)", "enabled": False,
                     "present": True})
    if as_json:
        print(json.dumps({"zim_dir": str(cfg.zim_dir), "zims": rows}, indent=2))
        return EXIT_OK
    print(f"zim_dir: {cfg.zim_dir}")
    if not rows:
        print("  (no zims configured and none on disk)")
    for row in rows:
        flags = ("enabled" if row["enabled"] else "disabled") + \
                ("" if row["present"] else ", MISSING on disk")
        print(f"  {row['file']}  [{flags}]  {row['name']}")
    return EXIT_OK


def _zim_check_update(cfg: Config, as_json: bool) -> int:
    """Ask library.kiwix.org whether a newer dump exists.  REPORT ONLY.

    This never downloads anything: ZIM dumps are tens of gigabytes and that is
    a deliberate human decision, not something a status command should start.
    """
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
            entry["latest"] = _latest_catalog_date(resp.text, family)
        except requests.exceptions.RequestException as exc:
            entry["error"] = f"catalog unreachable: {exc}"
        except ET.ParseError as exc:
            entry["error"] = f"catalog response was not XML: {exc}"
        if entry["latest"] and entry["local_date"]:
            entry["newer_available"] = entry["latest"] > entry["local_date"]
        results.append(entry)

    if as_json:
        print(json.dumps({"checked": results}, indent=2))
        return EXIT_OK

    if not results:
        print("no zims configured to check")
    for row in results:
        if row["error"]:
            print(f"  {row['file']}: {row['error']}")
        elif not row["latest"]:
            print(f"  {row['file']}: no catalog entry found for {row['family']!r}")
        elif row["newer_available"]:
            print(f"  {row['file']}: NEWER dump available ({row['latest']}, "
                  f"local {row['local_date']})")
        else:
            print(f"  {row['file']}: up to date (catalog {row['latest']}, "
                  f"local {row['local_date'] or 'undated'})")
    print("\n(report only — InfoForge never downloads a ZIM for you)")
    return EXIT_OK


def _latest_catalog_date(xml_text: str, family: str) -> str | None:
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


# -------------------------------------------------------------------- zimify

def cmd_zimify(args) -> int:
    cfg = None
    try:
        cfg = load_config(args.config)
    except InfoForgeError:
        # zimify is useful before any config exists; only the default output
        # directory depends on it.
        pass
    input_dir = Path(args.input_dir).expanduser()
    if args.output:
        output = Path(args.output).expanduser()
    else:
        base = cfg.zim_dir if cfg else Path.home() / "zims"
        output = base / f"{input_dir.resolve().name}.zim"

    report = zimify(input_dir, output, title=args.title or "",
                    description=args.description,
                    creator=args.creator, publisher=args.publisher)
    if report["built"]:
        print(f"built {report['zim']} from {report['files']} file(s)")
        print(f"  prepared HTML kept at {report['prep_dir']} (safe to delete)")
        print("  add it to the zims: list in your config, then: infoforge serve recreate")
    else:
        print("zimwriterfs is not installed, so the .zim was NOT built.")
        print(f"  prepared HTML site: {report['prep_dir']}")
        print("  install zimwriterfs (zim-tools), then run from that directory:")
        # shlex.join so a title/description containing spaces stays one
        # argument when the printed command is pasted into a shell.
        print("    " + shlex.join(report["command"]))
    event("zimify", built=report["built"], files=report["files"])
    return EXIT_OK


# --------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="infoforge",
        description="Ask questions grounded in local Kiwix ZIM archives.",
    )
    parser.add_argument("--version", action="version",
                        version=f"infoforge {__version__}")
    parser.add_argument("--config", metavar="PATH",
                        help="config file to use (default: $INFOFORGE_CONFIG or "
                             "~/.config/infoforge/config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="answer a question from local ZIM sources")
    ask.add_argument("question", nargs="+")
    ask.add_argument("--json", action="store_true", help="machine-readable output")
    ask.add_argument("--strict", action="store_true",
                     help="exit 4 if the answer cites none of the sources "
                          "(exit 6, not-in-sources, applies with or without it)")
    ask.add_argument("--max-articles", type=int, metavar="N",
                     help="override max_articles from the config")
    ask.set_defaults(func=cmd_ask)

    search = sub.add_parser("search", help="retrieval only: show the sources")
    search.add_argument("question", nargs="+")
    search.add_argument("--json", action="store_true")
    search.add_argument("--text", action="store_true",
                        help="with --json: include each source's extracted text")
    search.add_argument("--max-articles", type=int, metavar="N")
    search.set_defaults(func=cmd_search)

    serve = sub.add_parser("serve", help="kiwix-serve container lifecycle")
    serve.add_argument("action", choices=("start", "stop", "status", "recreate"))
    serve.add_argument("--json", action="store_true")
    serve.set_defaults(func=cmd_serve)

    status = sub.add_parser("status", help="container + endpoint status")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="check every link in the chain")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    zim = sub.add_parser("zim", help="ZIM registry")
    zim.add_argument("action", choices=("list", "check-update"))
    zim.add_argument("--json", action="store_true")
    zim.set_defaults(func=cmd_zim)

    zf = sub.add_parser("zimify", help="build a .zim from a folder of docs")
    zf.add_argument("input_dir")
    zf.add_argument("-o", "--output", metavar="FILE",
                    help="output .zim path (default: <zim_dir>/<dirname>.zim)")
    zf.add_argument("--title")
    zf.add_argument("--description", default="Local knowledge base")
    zf.add_argument("--creator", default="InfoForge",
                    help="zimwriterfs --creator (mandatory metadata)")
    zf.add_argument("--publisher", default="InfoForge",
                    help="zimwriterfs --publisher (mandatory metadata)")
    zf.set_defaults(func=cmd_zimify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except InfoForgeError as exc:
        print(f"infoforge: error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("infoforge: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
