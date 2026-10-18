"""Append-only jsonl event log at ~/.local/state/infoforge/log.jsonl.

Deliberately dumb: one JSON object per line, best-effort, never fatal.  A
broken or unwritable log must not take an answer down with it, so every failure
here is swallowed.

Privacy: the file is created mode 0600 (its directory 0700), rotated at
``MAX_BYTES`` keeping ``KEEP`` old files, and holds no question text unless
``log_questions: true`` — by default only a keyed digest (HMAC-SHA-256 with a
random per-install key kept 0600 beside the log) and the length are stored
(see :func:`question_fields`).

Secrets never reach this file: callers pass endpoint *identifiers* (model_id,
base_url), never api_key values, and :func:`event` drops any field whose name
looks like a credential as a second line of defence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path

DEFAULT_LOG_PATH = Path("~/.local/state/infoforge/log.jsonl")

#: Rotate when the log reaches this size; keep this many rotated files.
MAX_BYTES = 1_000_000
KEEP = 3

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


#: The per-install key for question digests, created beside the log.
KEY_NAME = "question.key"
_KEYS: dict[str, bytes] = {}


def _question_key() -> bytes | None:
    """A random 32-byte key, created once (mode 0600) next to the log file.

    A plain SHA-256 of a question is not private: questions are short and
    guessable, so anyone holding the log could confirm "was X asked?" by
    hashing X.  Keyed, the digest still shows repeats but proves nothing
    without the key.  None (digest omitted) if the key cannot be read or made.
    """
    path = log_path().with_name(KEY_NAME)
    cached = _KEYS.get(str(path))
    if cached:
        return cached
    for _ in range(2):
        try:
            key = path.read_bytes()
        except FileNotFoundError:
            key = None
        except OSError:
            return None
        if key is not None:
            if len(key) < 16:
                return None  # truncated or being written by another process
            _KEYS[str(path)] = key
            return key
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue  # another process created it first: read theirs
        except OSError:
            return None
        with os.fdopen(fd, "wb") as fh:
            fh.write(secrets.token_bytes(32))
    return None


def question_fields(question: str, keep: bool = False) -> dict:
    """The log fields describing a question.

    By default only a keyed digest (enough to spot repeats, useless for
    confirming a guessed question) and the length are kept; the text itself is
    stored only when ``log_questions`` is on.  Never raises.
    """
    if keep:
        return {"question": question}
    out: dict = {}
    key = _question_key()
    if key is not None:
        out["question_hmac"] = hmac.new(key, question.encode("utf-8"),
                                        hashlib.sha256).hexdigest()[:16]
    out["question_chars"] = len(question)
    return out


def _rotate(path: Path) -> None:
    """Shift ``log.jsonl`` -> ``.1`` -> ``.2`` ... once it is over MAX_BYTES."""
    try:
        if path.stat().st_size < MAX_BYTES:
            return
    except OSError:
        return
    for i in range(KEEP, 0, -1):
        src = path if i == 1 else path.with_name(f"{path.name}.{i - 1}")
        dst = path.with_name(f"{path.name}.{i}")
        if src.exists():
            os.replace(src, dst)


def event(kind: str, **fields) -> None:
    """Append one event.  Best effort — logging never raises."""
    record = {"ts": time.time(), "event": kind}
    record.update(_scrub(fields))
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _rotate(path)
        line = json.dumps(record, default=str, ensure_ascii=False)
        # 0600 at creation, so there is no window where it is world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if os.name == "posix" and (path.stat().st_mode & 0o077):
            os.chmod(path, 0o600)  # an older, looser file is tightened too
    except Exception:  # pragma: no cover - logging is never fatal
        pass


def warn(msg: str) -> None:
    """Human-facing warning on stderr (stdout stays the answer channel)."""
    print(f"infoforge: warning: {msg}", file=sys.stderr)


def note(msg: str) -> None:
    """Human-facing progress note on stderr."""
    print(f"infoforge: {msg}", file=sys.stderr)
