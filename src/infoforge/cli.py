"""Command line interface: a thin printer over :mod:`infoforge.api`.

Exit codes are a contract other tools depend on (the table is in
:mod:`infoforge.errors`):
  0 ok · 2 config/usage · 3 nothing to ground on · 4 --strict citation failure
  5 every LLM unreachable · 6 the model said the sources lack the answer.

`ask --json` prints one JSON object on every exit except a crash, always with
``ok``, ``exit_code``, ``reason`` and ``sources``; failures add ``error``
(``{code, message, fix, docs}``).  A wrapper never has to parse prose or cope
with an empty stdout.

Note what exit 5 does *not* do: it still prints the sources that were found.
Retrieval and answering are separate halves, and a working search is useful on
its own when the brain is down.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from . import __version__, api, kiwix
from . import answer as answer_mod
from . import doctor as doctor_mod
from . import settings_schema as schema
from .config import (
    EXIT_CITATION,
    EXIT_NOT_IN_SOURCES,
    EXIT_OK,
    Config,
    InfoForgeError,
)
from .config import load as load_config
from .errors import (
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    REASON_BY_EXIT,
    REASON_OTHER,
    error_info,
)
from .logging_ import event, note
from .zimify import zimify

CATALOG_URL = api.CATALOG_URL

# Kept for callers that imported the 2.0 name.
REASONS = REASON_BY_EXIT


# ------------------------------------------------------------------- helpers

def _dump(payload: dict) -> None:
    print(json.dumps(payload, indent=2))


def _print_sources(sources: list[dict], stream=None) -> None:
    # Resolved at call time, not import time, so a redirected stdout is honoured.
    stream = stream or sys.stdout
    print("Citations:", file=stream)
    for i, src in enumerate(sources, 1):
        print(f"[{i}] {src['title']} — {src['url']}", file=stream)


# ---------------------------------------------------------------------- ask

def cmd_ask(args) -> int:
    question = " ".join(args.question).strip()
    result = api.ask(question, strict=args.strict, max_articles=args.max_articles,
                     config=args.config, notify=None if args.json else note)
    code = result["exit_code"]
    error = result.get("error")

    if args.json:
        _dump(result)
    elif "answer" in result:
        print(result["answer"])
        print()
        if result["uncited"] and result["verdict"] != "not_in_sources":
            print(answer_mod.UNCITED_WARNING)
            print()
        _print_sources(result["sources"])
        if result.get("attribution"):
            print()
            print(f"Attribution: {result['attribution']}")
    elif result["sources"]:
        # Exit 5 (every LLM down): the sources are still worth having.
        _print_sources(result["sources"], stream=sys.stdout)

    if code == EXIT_NOT_IN_SOURCES:
        print("infoforge: not in the sources: the retrieved articles do not "
              "contain the answer (nothing was invented)", file=sys.stderr)
    elif code == EXIT_CITATION:
        print("infoforge: strict mode: answer cited no sources", file=sys.stderr)
    elif error:
        print(f"infoforge: error: {error['message']}", file=sys.stderr)
    return code


# -------------------------------------------------------------------- search

def cmd_search(args) -> int:
    question = " ".join(args.question).strip()
    try:
        result = api.search(question, max_articles=args.max_articles, text=args.text,
                            ensure=not args.no_ensure, config=args.config)
    except InfoForgeError as exc:
        if args.json:
            _dump({"ok": False, "exit_code": exc.exit_code,
                   "reason": REASON_BY_EXIT.get(exc.exit_code, REASON_OTHER),
                   "query": question, "error": error_info(exc), "sources": []})
        raise
    if args.json:
        _dump(result)
    else:
        for i, src in enumerate(result["sources"], 1):
            print(f"[{i}] {src['title']}  (score {src['score']:.1f}, {src['chars']} chars)")
            print(f"    {src['url']}")
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
    info = api.status(cfg=cfg)
    if as_json:
        _dump(info)
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
    return _print_status(load_config(args.config), args.json)


# -------------------------------------------------------------------- doctor

def cmd_doctor(args) -> int:
    report = api.doctor(config=args.config)
    if args.json:
        _dump(report)
    else:
        print(doctor_mod.render(report))
    return EXIT_OK if report["ok"] else EXIT_FAILED


# ----------------------------------------------------------------------- zim

def cmd_zim(args) -> int:
    if args.action == "list":
        return _zim_list(api.zim_list(config=args.config), args.json)
    return _zim_check_update(api.zim_check_update(config=args.config), args.json)


def _zim_list(result: dict, as_json: bool) -> int:
    if as_json:
        _dump(result)
        return EXIT_OK
    rows = result["zims"]
    print(f"zim_dir: {result['zim_dir']}")
    if not rows:
        print("  (no zims configured and none on disk)")
    for row in rows:
        flags = ("enabled" if row["enabled"] else "disabled") + \
                ("" if row["present"] else ", MISSING on disk")
        print(f"  {row['file']}  [{flags}]  {row['name']}")
    return EXIT_OK


def _zim_check_update(result: dict, as_json: bool) -> int:
    if as_json:
        _dump(result)
        return EXIT_OK
    results = result["checked"]
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


# -------------------------------------------------------------------- config

def cmd_config(args) -> int:
    if args.action == "schema":
        if args.json:
            _dump(api.config_schema())
            return EXIT_OK
        for row in schema.schema_rows():
            bound = f", {schema.BY_KEY[row['key']].range_text()}" \
                if row.get("minimum") is not None else ""
            req = "required" if row["required"] else f"default {row['default']!r}"
            print(f"{row['key']}  ({row['type']}{bound}; {req}; env {row['env']}; "
                  f"{row['level']})")
            print(f"    {row['description']}")
        return EXIT_OK

    shown = api.config_show(config=args.config)
    if args.json:
        _dump(shown)
        return EXIT_OK
    print(f"config file: {shown['config_file'] or '(none: environment only)'}")
    for row in shown["settings"]:
        value = row["value"]
        if isinstance(value, (list, dict)):
            value = json.dumps(value)
        print(f"  {row['key']:<16} = {value}   [{row['source']}]")
    print("\nsource: cli > env (INFOFORGE_<KEY>) > file > default; secrets shown as ***")
    return EXIT_OK


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

#: Flags each question-taking subcommand knows: name -> number of values it takes.
_QUERY_FLAGS: dict[str, dict[str, int]] = {
    "ask": {"--json": 0, "--strict": 0, "--max-articles": 1, "-h": 0, "--help": 0},
    "search": {"--json": 0, "--text": 0, "--no-ensure": 0, "--max-articles": 1,
               "-h": 0, "--help": 0},
}


def protect_query(argv: list[str]) -> list[str]:
    """Rewrite ``ask``/``search`` argv so the question can never be read as a flag.

    Only the subcommand's *own* known flags are flags; every other token — even
    one starting with ``-`` such as ``-5 degrees in kelvin?`` or ``--version``
    or ``--config=evil.yaml`` — belongs to the question.  A literal ``--``
    ends flag parsing explicitly.  Flags may still come before or after the
    question (``infoforge ask "q" --json``).  The result is argv with the
    flags first, then ``--``, then the question words, which argparse takes
    verbatim.
    """
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--config":
            i += 2
        elif tok.startswith("--config=") or tok in ("--version", "-h", "--help"):
            i += 1
        else:
            break
    if i >= len(argv) or argv[i] not in _QUERY_FLAGS:
        return argv
    known = _QUERY_FLAGS[argv[i]]

    flags: list[str] = []
    words: list[str] = []
    j = i + 1
    while j < len(argv):
        tok = argv[j]
        if tok == "--":
            words.extend(argv[j + 1:])
            break
        name, has_value, _ = tok.partition("=")
        if tok.startswith("-") and name in known and (not has_value or known[name] == 1):
            flags.append(tok)
            if known[name] == 1 and not has_value and j + 1 < len(argv):
                j += 1
                flags.append(argv[j])
        else:
            words.append(tok)
        j += 1
    return [*argv[:i + 1], *flags, "--", *words]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="infoforge",
        description="Ask questions grounded in local Kiwix ZIM archives.",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version",
                        version=f"infoforge {__version__}")
    parser.add_argument("--config", metavar="PATH",
                        help="config file to use (default: $INFOFORGE_CONFIG, then "
                             "$XDG_CONFIG_HOME/infoforge/config.yaml, then "
                             "~/.config/infoforge/config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="answer a question from local ZIM sources",
                         allow_abbrev=False)
    ask.add_argument("question", nargs="+",
                     help="the question; anything that is not an ask flag is part of it "
                          "(use -- to be explicit)")
    ask.add_argument("--json", action="store_true", help="machine-readable output")
    ask.add_argument("--strict", action="store_true",
                     help="exit 4 if the answer cites none of the sources "
                          "(exit 6, not-in-sources, applies with or without it)")
    ask.add_argument("--max-articles", type=int, metavar="N",
                     help="override max_articles from the config")
    ask.set_defaults(func=cmd_ask)

    search = sub.add_parser("search", help="retrieval only: show the sources",
                            allow_abbrev=False)
    search.add_argument("question", nargs="+",
                        help="the query; anything that is not a search flag is part of it "
                             "(use -- to be explicit)")
    search.add_argument("--json", action="store_true", help="machine-readable output")
    search.add_argument("--text", action="store_true",
                        help="with --json: include each source's extracted text")
    search.add_argument("--no-ensure", action="store_true",
                        help="never create/start the kiwix container: fail fast "
                             "(exit 2, code kiwix_unreachable) if kiwix-serve is down")
    search.add_argument("--max-articles", type=int, metavar="N",
                        help="override max_articles from the config")
    search.set_defaults(func=cmd_search)

    serve = sub.add_parser("serve", help="kiwix-serve container lifecycle")
    serve.add_argument("action", choices=("start", "stop", "status", "recreate"))
    serve.add_argument("--json", action="store_true", help="machine-readable (status only)")
    serve.set_defaults(func=cmd_serve)

    status = sub.add_parser("status", help="container + endpoint status")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="check every link in the chain")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.set_defaults(func=cmd_doctor)

    zim = sub.add_parser("zim", help="ZIM registry")
    zim.add_argument("action", choices=("list", "check-update"))
    zim.add_argument("--json", action="store_true", help="machine-readable output")
    zim.set_defaults(func=cmd_zim)

    config = sub.add_parser("config", help="show the effective settings or the schema")
    config.add_argument("action", choices=("show", "schema"))
    config.add_argument("--json", action="store_true", help="machine-readable output")
    config.set_defaults(func=cmd_config)

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
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(protect_query(raw))
    try:
        return args.func(args)
    except InfoForgeError as exc:
        print(f"infoforge: error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("infoforge: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
