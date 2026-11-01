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

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from collections.abc import Iterator
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
    _scrub_plaintext_once()
    out: dict = {}
    key = _question_key()
    if key is not None:
        out["question_hmac"] = hmac.new(key, question.encode("utf-8"),
                                        hashlib.sha256).hexdigest()[:16]
    out["question_chars"] = len(question)
    return out


#: A rotation/scrub lock older than this is assumed to be left by a dead process.
LOCK_STALE_S = 60.0


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[bool]:
    """Hold ``<log>.lock`` (created O_EXCL) while rotating or scrubbing.

    Yields True when we own the lock, False when another writer does (the
    caller skips the work — that writer is doing it).  A lock older than
    LOCK_STALE_S is removed and taken once more.  Never raises.
    """
    lock = path.with_name(path.name + ".lock")
    owned = False
    for attempt in range(2):
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if attempt == 0:
                try:
                    if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                        lock.unlink()
                        continue
                except OSError:
                    pass
            break
        except OSError:
            break
        os.close(fd)
        owned = True
        break
    try:
        yield owned
    finally:
        if owned:
            with contextlib.suppress(OSError):
                lock.unlink()


def _rotate(path: Path) -> None:
    """Shift ``log.jsonl`` -> ``.1`` -> ``.2`` ... once it is over MAX_BYTES.

    Done under a lock file so concurrent writers cannot double-rotate; a writer
    that cannot take the lock simply skips rotation this time.
    """
    try:
        if path.stat().st_size < MAX_BYTES:
            return
    except OSError:
        return
    with _file_lock(path) as owned:
        if not owned:
            return
        try:
            if path.stat().st_size < MAX_BYTES:
                return  # another writer rotated between our check and the lock
            for i in range(KEEP, 0, -1):
                src = path if i == 1 else path.with_name(f"{path.name}.{i - 1}")
                dst = path.with_name(f"{path.name}.{i}")
                if src.exists():
                    os.replace(src, dst)
        except OSError:
            pass


# Field names 2.0 wrote in plaintext: the question, a SHA-256 of it (2.1
# pre-HMAC builds), and article titles (``titles`` on retrieve, ``cited`` on ask).
_PLAINTEXT_MARKERS = (b'"question":', b'"question_sha256"', b'"titles":', b'"cited":')
_scrub_done = False


def _scrub_record(rec: dict) -> bool:
    """Strip question text from one record in place; True if it changed."""
    changed = False
    q = rec.get("question")
    if q is not None:
        del rec["question"]
        rec.setdefault("question_chars", len(q) if isinstance(q, str) else 0)
        changed = True
    for name in ("question_sha256", "titles", "cited"):
        if name in rec:
            del rec[name]
            changed = True
    return changed


def _scrub_file(path: Path) -> int:
    """Rewrite one log file without plaintext; returns records changed."""
    try:
        raw = path.read_bytes()
    except OSError:
        return 0
    if not any(m in raw for m in _PLAINTEXT_MARKERS):
        return 0
    out: list[bytes] = []
    changed = 0
    for line in raw.splitlines(keepends=True):
        if any(m in line for m in _PLAINTEXT_MARKERS):
            try:
                rec = json.loads(line)
            except ValueError:
                rec = None
            if isinstance(rec, dict) and _scrub_record(rec):
                nl = b"\n" if line.endswith(b"\n") else b""
                line = json.dumps(rec, default=str, ensure_ascii=False).encode("utf-8") + nl
                changed += 1
        out.append(line)
    if not changed:
        return 0
    tmp = path.with_name(f"{path.name}.scrub-{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"".join(out))
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return 0
    return changed


def _scrub_plaintext_once() -> None:
    """While ``log_questions`` is off the log files must hold no question text.

    Runs at most once per process (cheap byte pre-check per file, JSON only on
    a hit).  Old records keep their length as ``question_chars``; no HMAC is
    computed for them.  Best effort, never raises.
    """
    global _scrub_done
    if _scrub_done:
        return
    try:
        path = log_path()
        files = [path] + [path.with_name(f"{path.name}.{i}") for i in range(1, KEEP + 1)]
        files = [f for f in files if f.exists()]
        n_files = n_records = 0
        if files:
            with _file_lock(path) as owned:
                if not owned:
                    return  # another writer holds it: retry on the next call
                for f in files:
                    n = _scrub_file(f)
                    if n:
                        n_files += 1
                        n_records += n
        _scrub_done = True
        if n_records:
            event("log_scrub", files=n_files, records=n_records)
    except Exception:  # pragma: no cover - logging is never fatal
        _scrub_done = True


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
