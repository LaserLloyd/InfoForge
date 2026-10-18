"""`infoforge doctor` — one pass over every link in the chain.

Each check returns a row; nothing raises.  The point of a doctor is to tell you
which link is broken, so a failing check must never abort the ones after it —
and a config that will not even parse still yields a report, not a traceback.
"""

from __future__ import annotations

import os
import posixpath
import re
import tempfile
from dataclasses import dataclass, field

import requests
import yaml

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
        if not check["ok"]:
            mark = "FAIL"
        else:
            mark = "WARN" if check["detail"].startswith("WARNING") else "PASS"
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


# Filesystems (as /proc/mounts names them) that cannot carry POSIX modes: a
# Windows or macOS bind mount into a Linux container presents every file as 777
# and ignores chmod.
_NO_POSIX_MODE_FS = frozenset({
    "9p", "cifs", "smb3", "smbfs", "drvfs", "virtiofs", "vboxsf",
    "fuse.grpcfuse", "fuse.osxfs", "fuse.virtiofs", "fuse.vmhgfs-fuse",
})

_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


def _unescape_mount_field(text: str) -> str:
    """/proc/mounts writes a space as an octal escape (and tab, newline, backslash)."""
    return _MOUNT_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), text)


def _mount_fstype(path, mounts_file: str = "/proc/mounts") -> str | None:
    """The filesystem type of the longest mount point that contains ``path``."""
    try:
        with open(mounts_file, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    raw = str(path)
    target = posixpath.normpath(raw if raw.startswith("/") else os.path.abspath(raw))
    best, best_type = "", None
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount = _unescape_mount_field(parts[1])
        inside = target == mount or mount == "/" or target.startswith(mount.rstrip("/") + "/")
        if inside and len(mount) >= len(best):
            best, best_type = mount, parts[2]
    return best_type


def _chmod_is_ignored(path) -> bool:
    """True when chmod 600 on a scratch file beside ``path`` does not stick.

    False when it does, and also False when the probe cannot run (a read-only
    mount, no permission): an inconclusive probe must not excuse a finding.
    """
    try:
        with tempfile.NamedTemporaryFile(dir=os.path.dirname(os.path.abspath(str(path)))) as tmp:
            os.chmod(tmp.name, 0o600)
            return bool(os.stat(tmp.name).st_mode & 0o077)
    except OSError:
        return False


def _mount_ignores_modes(path) -> bool:
    """Can the filesystem holding ``path`` not carry POSIX permission modes?"""
    return _mount_fstype(path) in _NO_POSIX_MODE_FS or _chmod_is_ignored(path)


def _file_holds_secret(path) -> bool | None:
    """Does the config *file itself* carry an api_key (top level or a fallback)?

    A key that arrives through ``INFOFORGE_API_KEY`` is in the environment, not
    in the file, and is none of this check's business.  None: unreadable.
    """
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(loaded, dict):
        return False
    if str(loaded.get("api_key") or "").strip():
        return True
    fallbacks = loaded.get("fallbacks")
    if isinstance(fallbacks, list):
        return any(isinstance(e, dict) and str(e.get("api_key") or "").strip()
                   for e in fallbacks)
    return False


def _any_key(cfg: Config) -> bool:
    return bool(cfg.api_key) or any(f.api_key for f in cfg.fallbacks)


def _check_config_permissions(report: Report, cfg: Config) -> None:
    """A secret in a group/world-readable config file is a finding (POSIX only).

    Only a key written *in the file* counts.  On a mount that cannot carry
    POSIX modes (a Windows bind mount shows 777) the finding is downgraded to a
    warning, because chmod cannot fix it: move the key to the environment.
    """
    if cfg.path is None or not _any_key(cfg) or not _IS_POSIX:
        return
    in_file = _file_holds_secret(cfg.path)
    if in_file is None:
        return
    if not in_file:
        report.add("config perms", True,
                   f"{cfg.path} holds no secret (the api_key comes from the "
                   "environment), so its mode does not matter")
        return
    try:
        mode = _file_mode(cfg.path)
    except OSError:
        return
    if not mode & 0o077:
        report.add("config perms", True, f"{cfg.path} is mode {mode:03o}")
    elif _mount_ignores_modes(cfg.path):
        report.add("config perms", True,
                   f"WARNING: {cfg.path} holds an api_key and shows mode {mode:03o}, but its "
                   "mount (a Windows/macOS bind mount?) cannot carry POSIX modes, so chmod "
                   "cannot fix it: move the key to the environment (INFOFORGE_API_KEY) and "
                   "blank api_key in the file")
    else:
        report.add("config perms", False,
                   f"{cfg.path} holds an api_key but is mode {mode:03o}: run "
                   f"chmod 600 {cfg.path}")


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
