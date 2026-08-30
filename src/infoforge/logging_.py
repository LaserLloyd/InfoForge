"""Append-only jsonl event log at ~/.local/state/infoforge/log.jsonl.

Deliberately dumb: one JSON object per line, best-effort, never fatal.  A
broken or unwritable log must not take an answer down with it, so every failure
here is swallowed.

Secrets never reach this file: callers pass endpoint *identifiers* (model_id,
base_url), never api_key values, and :func:`event` drops any field whose name
looks like a credential as a second line of defence.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

DEFAULT_LOG_PATH = Path("~/.local/state/infoforge/log.jsonl")

# Field names that must never be written, whatever a caller passes.
_SECRET_HINTS = ("api_key", "apikey", "token", "secret", "password", "authorization")


def log_path() -> Path:
    """Where events go (override with INFOFORGE_LOG for tests/ops)."""
    env = os.environ.get("INFOFORGE_LOG")
    return Path(env).expanduser() if env else DEFAULT_LOG_PATH.expanduser()


def _scrub(fields: dict) -> dict:
    return {
        k: v
        for k, v in fields.items()
        if not any(hint in k.lower() for hint in _SECRET_HINTS)
    }


def event(kind: str, **fields) -> None:
    """Append one event.  Best effort — logging never raises."""
    record = {"ts": time.time(), "event": kind}
    record.update(_scrub(fields))
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # pragma: no cover - logging is never fatal
        pass


def warn(msg: str) -> None:
    """Human-facing warning on stderr (stdout stays the answer channel)."""
    print(f"infoforge: warning: {msg}", file=sys.stderr)


def note(msg: str) -> None:
    """Human-facing progress note on stderr."""
    print(f"infoforge: {msg}", file=sys.stderr)
