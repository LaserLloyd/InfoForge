"""`infoforge doctor` — one pass over every link in the chain.

Each check returns a row; nothing raises.  The point of a doctor is to tell you
which link is broken, so a failing check must never abort the ones after it —
and a config that will not even parse still yields a report, not a traceback.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import requests

from . import kiwix
from .config import Config, ConfigError
from .config import load as load_config
from .retrieve import keyword_query

# A query that any general encyclopedia ZIM answers; used only to prove that
# the full-text index responds, not to test relevance.
PROBE_QUERY = "water"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def render(self) -> str:
        width = max((len(c.name) for c in self.checks), default=10)
        lines = []
        for check in self.checks:
            mark = "PASS" if check.ok else "FAIL"
            lines.append(f"  [{mark}] {check.name.ljust(width)}  {check.detail}")
        lines.append("")
        lines.append("doctor: " + ("all checks passed" if self.ok else "problems found"))
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail}
                       for c in self.checks],
        }


def _check_llm(report: Report, label: str, endpoint, timeout: float,
               session: requests.Session | None = None) -> None:
    """A tiny GET /models — cheap, and it does not spin up a model."""
    get = (session or requests).get
    headers = {}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    try:
        resp = get(endpoint.models_url, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        report.add(label, False, f"{endpoint.base_url} unreachable: {exc}")
        return
    if resp.status_code >= 400:
        report.add(label, False,
                   f"{endpoint.base_url} answered HTTP {resp.status_code}")
        return
    served = ""
    try:
        data = resp.json().get("data", [])
        ids = [str(m.get("id")) for m in data if isinstance(m, dict)]
        if ids:
            served = " (model listed)" if endpoint.model_id in ids else \
                     " (WARNING: model_id not in /models list)"
    except ValueError:
        pass
    report.add(label, True, f"{endpoint.base_url} reachable, model "
                            f"{endpoint.model_id}{served}")


def run(explicit_config: str | None = None,
        session: requests.Session | None = None) -> Report:
    """Run every check and return the report (never raises)."""
    report = Report()

    try:
        cfg: Config | None = load_config(explicit_config)
    except ConfigError as exc:
        # Keep the pointed message (advice lines and all) but stay a report,
        # not a traceback: a missing config is the most common doctor result.
        lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
        report.add("config", False, "\n      ".join(lines))
        return report

    report.add("config", True, f"parsed {cfg.path}")

    # --- ZIM files
    if not cfg.zim_dir.is_dir():
        report.add("zim_dir", False, f"{cfg.zim_dir} does not exist")
    else:
        report.add("zim_dir", True, str(cfg.zim_dir))

    enabled = cfg.enabled_zims
    if not enabled:
        report.add("zims", False, "no enabled entries in the config's zims: list")
    else:
        missing = [z.file for z in enabled if not (cfg.zim_dir / z.file).is_file()]
        if missing:
            report.add("zims", False, f"enabled but missing on disk: {', '.join(missing)}")
        else:
            report.add("zims", True,
                       f"{len(enabled)} enabled: {', '.join(z.file for z in enabled)}")

    # --- container
    try:
        state = kiwix.container_state()
        serving = kiwix._serving_files() if state != "missing" else []
        report.add("container", state == "running",
                   f"{kiwix.CONTAINER_NAME}: {state}"
                   + (f", serving {', '.join(serving)}" if serving else ""))
    except Exception as exc:
        report.add("container", False, str(exc).splitlines()[0])

    # --- kiwix HTTP
    if kiwix.is_up(cfg, session=session):
        report.add("kiwix http", True, f"{cfg.kiwix_url} responds")
    else:
        report.add("kiwix http", False, f"{cfg.kiwix_url} does not respond")

    # --- full-text search
    if enabled:
        book = enabled[0].books_name
        hits = kiwix.search(cfg, book, keyword_query(PROBE_QUERY), 3, session=session)
        report.add("search", bool(hits),
                   f"{len(hits)} hit(s) for {PROBE_QUERY!r} in {book}")

    # --- the LLM chain
    _check_llm(report, "llm primary", cfg.primary, min(cfg.timeout_s, 15), session)
    for i, fallback in enumerate(cfg.fallbacks, 1):
        _check_llm(report, f"llm fallback {i}", fallback, min(cfg.timeout_s, 15), session)

    return report
