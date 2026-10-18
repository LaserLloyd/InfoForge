"""The error catalog: one table for every exit code, ``reason`` string and error code.

Other tools script against InfoForge, so these are contracts, not prose:

* the **exit code** a process returns (``EXIT_*``);
* the **reason** string ``ask --json`` prints beside it (one per exit code, so
  ``reason: "config"`` stays overloaded exactly as it was in 2.0 — callers
  that depend on it keep working);
* the **code**, a finer-grained, stable identifier added in 2.1, which says
  *which* config/usage/infrastructure problem it was.  ``--json`` output and
  the library API carry ``error: {code, message, fix, docs}``.

Add a new failure by adding a row to :data:`CATALOG`; ``docs/reference/errors.md``
is generated from this table (``scripts/gen_docs.py``) and a test fails when the
two drift.  This module imports nothing from the rest of the package so every
other module can import it.
"""

from __future__ import annotations

from dataclasses import dataclass

# ------------------------------------------------------------- exit contract
EXIT_OK = 0
EXIT_FAILED = 1        # `doctor` found a failing check, or an unexpected crash
EXIT_CONFIG = 2        # bad config or bad usage (also podman/kiwix/zimify/LLM-4xx errors)
EXIT_NO_GROUNDING = 3  # retrieval found nothing to ground on
EXIT_CITATION = 4      # --strict and the answer cited none of the sources
EXIT_LLM = 5           # every LLM in the chain was unreachable
EXIT_NOT_IN_SOURCES = 6  # the model said the retrieved sources lack the answer
EXIT_INTERRUPTED = 130  # Ctrl-C

#: ``reason`` in ``ask --json``: exactly one string per exit code (stable).
REASON_BY_EXIT: dict[int, str] = {
    EXIT_OK: "answered",
    EXIT_CONFIG: "config",
    EXIT_NO_GROUNDING: "no_sources",
    EXIT_CITATION: "uncited",
    EXIT_LLM: "llm_unreachable",
    EXIT_NOT_IN_SOURCES: "not_in_sources",
}
#: Fallback reason for an exit code with no row above (e.g. 1, 130).
REASON_OTHER = "error"

DOCS_PAGE = "docs/reference/errors.md"


@dataclass(frozen=True)
class ErrorSpec:
    """One row of the catalog."""

    code: str      # stable identifier, lower_snake_case
    exit: int      # process exit code
    message: str   # what happened (a template; callers usually pass their own detail)
    fix: str       # what to do about it
    summary: str   # one line for the docs table

    @property
    def reason(self) -> str:
        return REASON_BY_EXIT.get(self.exit, REASON_OTHER)

    @property
    def docs(self) -> str:
        """Docs slug: the reference page plus the anchor for this code."""
        return f"{DOCS_PAGE}#{self.code}"


def _spec(code: str, exit_code: int, summary: str, message: str, fix: str) -> ErrorSpec:
    return ErrorSpec(code=code, exit=exit_code, message=message, fix=fix, summary=summary)


_ROWS = (
    _spec("ok", EXIT_OK,
          "Answered, grounded in retrieved sources (or a search that found sources).",
          "Done.", "Nothing to fix."),
    _spec("internal_error", EXIT_FAILED,
          "An unexpected crash inside InfoForge.",
          "InfoForge hit an unexpected error: {detail}",
          "Run `infoforge doctor`, then report the traceback (see SECURITY.md for "
          "anything sensitive)."),
    _spec("doctor_failed", EXIT_FAILED,
          "`infoforge doctor` reported at least one failing check.",
          "One or more health checks failed.",
          "Read the first FAIL row of `infoforge doctor` and fix that link first."),
    _spec("usage_error", EXIT_CONFIG,
          "The command line was wrong (for example an empty question).",
          "{detail}",
          "Run `infoforge <command> --help`. A question that starts with '-' is fine; "
          "put `--` before it to be explicit."),
    _spec("config_missing", EXIT_CONFIG,
          "No config file exists at the resolved path.",
          "No config file at {detail}.",
          "Copy config.example.yaml to ~/.config/infoforge/config.yaml (or set "
          "INFOFORGE_CONFIG / --config), then set model_id and base_url."),
    _spec("config_unreadable", EXIT_CONFIG,
          "The config file exists but cannot be read or parsed as YAML.",
          "Cannot read the config file: {detail}",
          "Check the file's permissions and that it is valid YAML."),
    _spec("config_invalid", EXIT_CONFIG,
          "A config value (or INFOFORGE_* environment override) failed validation.",
          "{detail}",
          "Run `infoforge config schema` for the allowed values of every key."),
    _spec("kiwix_unreachable", EXIT_CONFIG,
          "kiwix-serve does not answer at kiwix_url.",
          "kiwix-serve does not answer at {detail}.",
          "Start it with `infoforge serve start`, or point kiwix_url at a running "
          "kiwix-serve and avoid `serve`."),
    _spec("kiwix_no_zims", EXIT_CONFIG,
          "No enabled ZIM from the zims: list exists in zim_dir.",
          "No enabled .zim files found in {detail}.",
          "Put a .zim in zim_dir and add it to zims: with enabled: true."),
    _spec("podman_missing", EXIT_CONFIG,
          "podman is not installed (needed only to run the kiwix container).",
          "podman is not installed or not on PATH.",
          "Install podman, or run your own kiwix-serve and set kiwix_url (then "
          "`serve` is not needed)."),
    _spec("container_failed", EXIT_CONFIG,
          "podman could not create, start or stop the kiwix container.",
          "{detail}",
          "Run `podman logs infoforge-kiwix` and `infoforge doctor`."),
    _spec("llm_rejected", EXIT_CONFIG,
          "The LLM endpoint rejected the request itself (a 4xx that would fail "
          "identically on every endpoint).",
          "{detail}",
          "Check model_id, base_url and api_key against what the server serves."),
    _spec("zimify_failed", EXIT_CONFIG,
          "`infoforge zimify` could not prepare or build the archive.",
          "{detail}",
          "Check the input directory and that pandoc/zimwriterfs are installed."),
    _spec("no_sources", EXIT_NO_GROUNDING,
          "Retrieval found nothing relevant, so nothing was answered.",
          "No sources found for the question.",
          "Try fewer/plainer keywords, or run `infoforge doctor` to confirm the "
          "ZIM is being served."),
    _spec("uncited", EXIT_CITATION,
          "`--strict`: the answer cited none of the sources it was given.",
          "Strict mode: the answer cited no sources.",
          "Treat the answer as ungrounded; retry, raise max_chars or use a "
          "stronger model."),
    _spec("llm_unreachable", EXIT_LLM,
          "Every LLM endpoint in the chain was unreachable.",
          "All LLM endpoints are unreachable.",
          "Check that the server at base_url is up and model_id is the name it "
          "serves; the retrieved sources are still printed."),
    _spec("not_in_sources", EXIT_NOT_IN_SOURCES,
          "Articles were found but the model says they do not contain the answer.",
          "The retrieved articles do not contain the answer (nothing was invented).",
          "Report 'not in the sources' to the user; do not fall back to memory."),
    _spec("interrupted", EXIT_INTERRUPTED,
          "The process was interrupted (Ctrl-C).",
          "Interrupted.", "Re-run the command."),
)

CATALOG: dict[str, ErrorSpec] = {row.code: row for row in _ROWS}

#: The code used when an exception carries none of its own, per exit code.
DEFAULT_CODE_BY_EXIT: dict[int, str] = {
    EXIT_FAILED: "internal_error",
    EXIT_CONFIG: "usage_error",
    EXIT_NO_GROUNDING: "no_sources",
    EXIT_CITATION: "uncited",
    EXIT_LLM: "llm_unreachable",
    EXIT_NOT_IN_SOURCES: "not_in_sources",
}


def spec_for(code: str | None, exit_code: int = EXIT_CONFIG) -> ErrorSpec:
    """The catalog row for ``code``, or the default row for ``exit_code``."""
    if code and code in CATALOG:
        return CATALOG[code]
    return CATALOG[DEFAULT_CODE_BY_EXIT.get(exit_code, "internal_error")]


# ----------------------------------------------------------------- exceptions
class InfoForgeError(Exception):
    """Base error carrying the exit code the CLI should return and a catalog code."""

    exit_code = EXIT_CONFIG
    code = "usage_error"

    def __init__(self, message: str = "", *, code: str | None = None,
                 fix: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self._fix = fix

    @property
    def fix(self) -> str:
        return self._fix or spec_for(self.code, self.exit_code).fix

    @property
    def docs(self) -> str:
        return spec_for(self.code, self.exit_code).docs


class ConfigError(InfoForgeError):
    """Config file missing, unparseable, or missing a required key."""

    exit_code = EXIT_CONFIG
    code = "config_invalid"


class NoGroundingError(InfoForgeError):
    """Retrieval produced no usable source text."""

    exit_code = EXIT_NO_GROUNDING
    code = "no_sources"


class LLMUnreachableError(InfoForgeError):
    """Primary and every fallback failed at the transport level."""

    exit_code = EXIT_LLM
    code = "llm_unreachable"


def error_info(exc: InfoForgeError) -> dict:
    """The ``error`` object of ``--json`` output and of the library API."""
    return {
        "code": exc.code,
        "message": str(exc),
        "fix": exc.fix,
        "docs": exc.docs,
    }


def info_for_code(code: str, message: str | None = None) -> dict:
    """An ``error`` object built straight from the catalog (no exception)."""
    spec = spec_for(code)
    return {
        "code": spec.code,
        "message": message or spec.message.format(detail=""),
        "fix": spec.fix,
        "docs": spec.docs,
    }
