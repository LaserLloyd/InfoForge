"""`infoforge doctor` — one pass over every link in the chain.

Each check returns a row; nothing raises.  The point of a doctor is to tell you
which link is broken, so a failing check must never abort the ones after it —
and a config that will not even parse still yields a report, not a traceback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import requests

from . import kiwix
from .config import Config, ConfigError
from .config import load as load_config
from .errors import info_for_code
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
        return render(self.to_dict())

    def to_dict(self) -> dict:
        out: dict = {
            "ok": self.ok,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail}
                       for c in self.checks],
        }
        if not self.ok:
            out["error"] = info_for_code("doctor_failed")
        return out


def render(report: dict) -> str:
    """The human table for a report dict (what ``Report.to_dict`` returns)."""
    checks = report["checks"]
    width = max((len(c["name"]) for c in checks), default=10)
    lines = []
    for check in checks:
        mark = "PASS" if check["ok"] else "FAIL"
        lines.append(f"  [{mark}] {check['name'].ljust(width)}  {check['detail']}")
    lines.append("")
    lines.append("doctor: " + ("all checks passed" if report["ok"] else "problems found"))
    return "\n".join(lines)


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


LOOPBACK_BINDS = ("127.0.0.1", "localhost", "::1")


_IS_POSIX = os.name == "posix"


def _file_mode(path) -> int:
    return path.stat().st_mode & 0o777


def _check_config_permissions(report: Report, cfg: Config) -> None:
    """An api_key in a group/world-readable file is a finding (POSIX only)."""
    if cfg.path is None or not cfg.api_key or not _IS_POSIX:
        return
    try:
        mode = _file_mode(cfg.path)
    except OSError:
        return
    if mode & 0o077:
        report.add("config perms", False,
                   f"{cfg.path} holds an api_key but is mode {mode:03o}: run "
                   f"chmod 600 {cfg.path}")
    else:
        report.add("config perms", True, f"{cfg.path} is mode {mode:03o}")


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

    report.add("config", True,
               f"parsed {cfg.path}" if cfg.path else "from INFOFORGE_* environment (no file)")
    _check_config_permissions(report, cfg)

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
    if not kiwix.podman_available():
        # Bring-your-own kiwix-serve: there is no container to check.  The
        # "kiwix http" row below is what decides whether this setup works.
        report.add("container", True,
                   "podman not installed: skipped (using the kiwix-serve at kiwix_url)")
    else:
        try:
            state = kiwix.container_state()
            serving = kiwix._serving_files() if state != "missing" else []
            report.add("container", state == "running",
                       f"{kiwix.CONTAINER_NAME}: {state}"
                       + (f", serving {', '.join(serving)}" if serving else ""))
        except Exception as exc:
            report.add("container", False, str(exc).splitlines()[0])

    # --- exposure: kiwix-serve has no authentication
    if cfg.kiwix_bind not in LOOPBACK_BINDS:
        report.add("kiwix exposure", True,
                   f"WARNING: kiwix_bind is {cfg.kiwix_bind}, so the whole library is "
                   "readable by anything that can reach this host (kiwix-serve has no "
                   "authentication); use 127.0.0.1 unless you mean it")

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
