"""kiwix-serve: rootless-podman container lifecycle + the search/fetch client.

Two halves that both talk to the same service:

* the container half (``ensure``/``start``/``stop``/``status``/``recreate``)
  is a port of the ~/localwiki bash launcher.  Every podman call is a fixed
  argv list handed to subprocess — never a shell string.
* the HTTP half (``suggest``/``search``/``fetch_article``) is the client
  localwiki.py grew against a real kiwix-serve build.

The one non-obvious HTTP fact: **search uses format=xml only**.  This kiwix
build answers ``format=json`` with the HTML search *page*, so the JSON path
silently produced zero results; localwiki.py papered over that with a
try-json-then-xml dance.  Here we ask for the format that actually works.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

from .config import Config
from .errors import InfoForgeError
from .logging_ import warn

CONTAINER_NAME = "infoforge-kiwix"
#: Image repository.  The tag is the ``kiwix_image_tag`` config key (pinned by
#: default, never an implicit ``latest``); see :func:`image_ref`.
IMAGE = "ghcr.io/kiwix/kiwix-serve"

# kiwix-serve binds inside the container on this port; we publish it outward.
CONTAINER_PORT = 8080

SEARCH_TIMEOUT_S = 20
SUGGEST_TIMEOUT_S = 15
FETCH_TIMEOUT_S = 30


class KiwixError(InfoForgeError):
    """Container or HTTP failure that the caller cannot work around."""

    code = "container_failed"


def image_ref(cfg: Config) -> str:
    """``ghcr.io/kiwix/kiwix-serve:<tag>`` for the configured tag."""
    return f"{IMAGE}:{cfg.kiwix_image_tag}"


def podman_available() -> bool:
    """True when a ``podman`` binary is on PATH.

    Without it the container half of InfoForge is simply unavailable, which is
    fine when ``kiwix_url`` points at a kiwix-serve you run yourself.
    """
    return shutil.which("podman") is not None


# --------------------------------------------------------------------- podman

def _podman(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    """Run podman with a fixed argv (never shell=True).

    stdin is /dev/null: under ``infoforge-mcp`` (``status``/``doctor`` tools)
    stdin is the MCP protocol stream, and a child must never inherit it.
    """
    try:
        return subprocess.run(
            ["podman", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=check,
        )
    except FileNotFoundError:
        raise KiwixError(
            "podman is not installed or not on PATH — InfoForge runs kiwix-serve "
            "as a rootless podman container (or set kiwix_url to a kiwix-serve "
            "you run yourself)",
            code="podman_missing",
        ) from None


def container_state(name: str = CONTAINER_NAME) -> str:
    """``missing`` | ``running`` | ``exited`` | ``created`` | ``paused`` | ``unknown``."""
    exists = _podman("container", "exists", name)
    if exists.returncode != 0:
        return "missing"
    got = _podman("inspect", "-f", "{{.State.Status}}", name)
    state = got.stdout.strip()
    return state or "unknown"


def _container_started_epoch(name: str = CONTAINER_NAME) -> float:
    """Epoch seconds the container was started at, 0.0 if unknown.

    podman emits e.g. ``2026-08-20 09:48:48.54468448 +0900 JST``: fractional
    seconds and a trailing timezone *name* that no stdlib parser accepts, so
    strip both before parsing (the bash launcher did the same with sed).
    """
    got = _podman("inspect", "-f", "{{.State.StartedAt}}", name)
    raw = got.stdout.strip()
    if not raw:
        return 0.0
    cleaned = re.sub(r"\.\d+", "", raw)
    cleaned = re.sub(r"\s+[A-Z]{2,5}$", "", cleaned).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(cleaned, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def _container_cmd(name: str = CONTAINER_NAME) -> list[str]:
    """The argv the container was created with (used to spot a ZIM-set change)."""
    got = _podman("inspect", "-f", "{{json .Config.Cmd}}", name)
    try:
        value = json.loads(got.stdout.strip() or "null")
    except ValueError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


# ----------------------------------------------------------------- zim set

def zim_files(cfg: Config) -> list[str]:
    """Enabled ZIM filenames that actually exist in ``zim_dir``.

    A configured-but-absent file is a warning, not a failure: a half-finished
    download should not stop the archives you *do* have from being served.
    """
    out: list[str] = []
    for zim in cfg.enabled_zims:
        if (cfg.zim_dir / zim.file).is_file():
            out.append(zim.file)
        else:
            warn(f"enabled zim not found on disk, skipping: {cfg.zim_dir / zim.file}")
    return out


def newest_zim_mtime(cfg: Config) -> float:
    """Newest mtime among the .zim files in zim_dir (0.0 if none)."""
    newest = 0.0
    if not cfg.zim_dir.is_dir():
        return 0.0
    for path in cfg.zim_dir.glob("*.zim"):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def _run_args(cfg: Config, files: list[str]) -> list[str]:
    """Full `podman run` argv for a fresh container."""
    bind = cfg.kiwix_bind or "127.0.0.1"
    return [
        "run", "-d",
        "--name", CONTAINER_NAME,
        # kiwix-serve's image entrypoint needs to read the bind-mounted ZIMs;
        # rootless podman maps this back to the invoking user on the host.
        "--user", "root",
        "-p", f"{bind}:{cfg.kiwix_port}:{CONTAINER_PORT}",
        "-v", f"{cfg.zim_dir}:/data:z",
        image_ref(cfg),
        "-k",
        *[f"/data/{name}" for name in files],
    ]


def _serving_files(name: str = CONTAINER_NAME) -> list[str]:
    """ZIM filenames the existing container was told to serve."""
    return [
        arg.rsplit("/", 1)[-1]
        for arg in _container_cmd(name)
        if arg.startswith("/data/")
    ]


def _create(cfg: Config, files: list[str]) -> None:
    result = _podman(*_run_args(cfg, files))
    if result.returncode != 0:
        err = (result.stderr or result.stdout).strip()
        # Race: the container appeared between our existence check and this
        # run.  Starting the existing one is the right answer, not an error.
        if "already in use" in err or "already exists" in err:
            started = _podman("start", CONTAINER_NAME)
            if started.returncode == 0:
                return
            err = (started.stderr or started.stdout).strip()
        raise KiwixError(f"failed to create container {CONTAINER_NAME}: {err}",
                         code="container_failed")


def recreate(cfg: Config) -> list[str]:
    """Force-remove and recreate the container; returns the served files."""
    files = zim_files(cfg)
    if not files:
        raise KiwixError(_no_zims_message(cfg), code="kiwix_no_zims")
    _podman("rm", "-f", CONTAINER_NAME)
    _create(cfg, files)
    return files


def _no_zims_message(cfg: Config) -> str:
    return (
        f"no enabled .zim files found in {cfg.zim_dir}\n"
        "  Either the download has not finished, or no entry in the config's "
        "zims: list is enabled.\n"
        f"  Put a .zim in {cfg.zim_dir} and add it to zims: with enabled: true."
    )


def ensure(cfg: Config) -> dict:
    """Make sure a container serving exactly the enabled ZIM set is running.

    Recreates when the enabled-ZIM *set* changed or when a .zim is newer than
    the container's start time — kiwix-serve reads its file list once at
    startup and does not hot-reload, so a finished download is invisible until
    the container is rebuilt.  Returns a small action report for the caller.
    """
    cfg.zim_dir.mkdir(parents=True, exist_ok=True)
    files = zim_files(cfg)
    if not files:
        raise KiwixError(_no_zims_message(cfg), code="kiwix_no_zims")

    state = container_state()
    if state == "missing":
        _create(cfg, files)
        return {"action": "created", "state": "running", "files": files}

    started = _container_started_epoch()
    newest = newest_zim_mtime(cfg)
    served = _serving_files()

    if sorted(served) != sorted(files):
        recreate(cfg)
        return {"action": "recreated", "reason": "zim set changed",
                "state": "running", "files": files}
    if started and newest > started:
        recreate(cfg)
        return {"action": "recreated", "reason": "newer zim on disk",
                "state": "running", "files": files}

    if state == "running":
        return {"action": "none", "state": "running", "files": files}

    result = _podman("start", CONTAINER_NAME)
    if result.returncode != 0:
        raise KiwixError(
            f"failed to start container {CONTAINER_NAME}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return {"action": "started", "state": "running", "files": files}


def start(cfg: Config) -> dict:
    return ensure(cfg)


def stop(cfg: Config | None = None) -> dict:
    state = container_state()
    if state == "missing":
        return {"action": "none", "state": "missing"}
    if state in ("exited", "stopped", "created"):
        return {"action": "none", "state": state}
    result = _podman("stop", CONTAINER_NAME)
    if result.returncode != 0:
        raise KiwixError(
            f"failed to stop container {CONTAINER_NAME}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return {"action": "stopped", "state": "exited"}


def status(cfg: Config) -> dict:
    """Container state plus what it is actually serving (not what config wants)."""
    state = container_state()
    info = {
        "name": CONTAINER_NAME,
        "state": state,
        "kiwix_url": cfg.kiwix_url,
        "configured": zim_files(cfg) if cfg.zim_dir.is_dir() else [],
        "serving": _serving_files() if state != "missing" else [],
    }
    if state != "missing":
        ps = _podman(
            "ps", "-a", "--filter", f"name={CONTAINER_NAME}",
            "--format", "{{.Image}} | {{.Status}} | {{.Ports}}",
        )
        info["ps"] = ps.stdout.strip()
    return info


# ------------------------------------------------------------------ HTTP side

@dataclass
class Hit:
    """One retrieval candidate."""

    url: str            # /content/<book>/<path>
    title: str
    snippet: str = ""
    from_suggest: bool = False
    score: float = 0.0

    @property
    def key(self) -> str:
        """Dedupe identity: the content path, case- and encoding-normalised."""
        return urllib.parse.unquote(self.url).rstrip("/").lower()


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def suggest(cfg: Config, books_name: str, phrase: str,
            session: requests.Session | None = None) -> Hit | None:
    """Exact-title lookup via /suggest, or None.

    /suggest is a title *prefix* index.  Entries with ``"kind": "path"`` are
    real articles; ``"kind": "pattern"`` entries are the "containing ..."
    pseudo-results the web UI shows, and are useless to us.  Matching is
    punctuation-insensitive so the phrase "Nineteen Eighty Four" resolves the
    real title "Nineteen Eighty-Four".
    """
    target = _norm(phrase)
    if not target:
        return None
    get = (session or requests).get
    try:
        resp = get(
            f"{cfg.kiwix_url}/suggest",
            params={"content": books_name, "term": phrase},
            timeout=SUGGEST_TIMEOUT_S,
        )
    except requests.exceptions.RequestException as exc:
        warn(f"suggest failed for {phrase!r}: {exc}")
        return None
    if resp.status_code != 200:
        return None
    try:
        items = resp.json()
    except ValueError:
        return None
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict) or item.get("kind") != "path":
            continue
        value = str(item.get("value") or "").strip()
        path = str(item.get("path") or "").strip()
        if value and path and _norm(value) == target:
            return Hit(url=f"/content/{books_name}/{path}", title=value,
                       from_suggest=True)
    return None


def parse_search_xml(text: str) -> list[Hit]:
    """Parse kiwix-serve's XML (RSS 2.0) search response.

    Items carry a <title>, a <link> whose *text* is the article path
    (``/content/<book>/<Article_Path>``) and a <description> holding the
    highlighted snippet.  Atom-style <entry>/<link href=...> is accepted too,
    in case a future build switches formats.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        warn(f"could not parse search response as XML: {exc}")
        return []

    hits: list[Hit] = []
    for entry in root.iter():
        if entry.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
            continue
        title = url = snippet = None
        for child in entry.iter():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "title" and title is None:
                title = (child.text or "").strip()
            elif tag == "link" and url is None:
                href = (child.get("href") or "").strip()
                if href:
                    url = href
                elif (child.text or "").strip().startswith("/"):
                    url = child.text.strip()
            elif tag == "id" and url is None:
                url = (child.text or "").strip()
            elif tag in ("description", "summary", "content") and snippet is None:
                snippet = "".join(child.itertext()).strip()
        if url and title:
            hits.append(Hit(url=url, title=title, snippet=snippet or ""))
    return hits


def search(cfg: Config, books_name: str, pattern: str, limit: int,
           session: requests.Session | None = None) -> list[Hit]:
    """Full-text search one book.

    ``format=xml`` is not a fallback here, it is the only format: this
    kiwix-serve build answers ``format=json`` with the HTML search page.
    ``books.name`` scopes the query to one book — without it kiwix-serve
    returns HTTP 400 as soon as two books in different languages are loaded.
    """
    get = (session or requests).get
    try:
        resp = get(
            f"{cfg.kiwix_url}/search",
            params={
                "pattern": pattern,
                "format": "xml",
                "books.name": books_name,
                "pageLength": str(limit),
            },
            timeout=SEARCH_TIMEOUT_S,
        )
    except requests.exceptions.RequestException as exc:
        warn(f"search failed for book {books_name!r}: {exc}")
        return []
    if resp.status_code != 200:
        warn(f"search failed for book {books_name!r}: HTTP {resp.status_code}")
        return []
    return parse_search_xml(resp.text)


def url_root(cfg: Config) -> str:
    """The path prefix of ``kiwix_url`` (``""``, or e.g. ``/kiwix`` behind a proxy).

    kiwix-serve started with ``--urlRootLocation=/kiwix`` prefixes every link it
    returns with that root, so it has to come off before the book is read.
    """
    return urllib.parse.urlparse(cfg.kiwix_url).path.rstrip("/")


def split_content_path(result_url: str, root: str = "") -> tuple[str | None, list[str]]:
    """Split ``[<root>]/content/<book>/<path>`` into (book, [encoded path parts])."""
    url = result_url.strip()
    if url.startswith("http"):
        url = urllib.parse.urlparse(url).path
    if root and url.startswith(root + "/content/"):
        url = url[len(root):]
    if not url.startswith("/content/"):
        url = "/content/" + url.lstrip("/")
    parts = [p for p in url.split("/") if p]
    if len(parts) < 2 or parts[0] != "content":
        return None, []
    book = urllib.parse.unquote(parts[1])
    # unquote-then-quote each component so an already-encoded path from the
    # search response does not get double-encoded on the way back out.
    article = [urllib.parse.quote(urllib.parse.unquote(p), safe="") for p in parts[2:]]
    return book, article


def quote_book(book: str) -> str:
    """Percent-encode a book name for use as ONE URL path segment.

    The book comes from a decoded search-result URL; unquoted, a ``%3F`` or
    ``%2F`` in it would turn into a query string or an extra path level when
    the URL is rebuilt.
    """
    return urllib.parse.quote(book, safe="")


def content_url(cfg: Config, result_url: str) -> str:
    """Human-clickable citation URL for a hit."""
    book, article = split_content_path(result_url, url_root(cfg))
    if book is None:
        return result_url
    return f"{cfg.kiwix_url}/content/{quote_book(book)}/{'/'.join(article)}"


def fetch_article(cfg: Config, result_url: str,
                  session: requests.Session | None = None) -> str | None:
    """Fetch an article's raw markup.

    ``/raw/<book>/content/<path>`` first (no kiwix chrome around the text),
    then the rendered ``/content/<book>/<path>`` page, because /raw 404s on
    some builds and entry types.
    """
    book, article = split_content_path(result_url, url_root(cfg))
    if book is None:
        warn(f"skipping result with unexpected URL: {result_url}")
        return None
    encoded = "/".join(article)
    get = (session or requests).get
    quoted_book = quote_book(book)
    for url in (
        f"{cfg.kiwix_url}/raw/{quoted_book}/content/{encoded}",
        f"{cfg.kiwix_url}/content/{quoted_book}/{encoded}",
    ):
        try:
            resp = get(url, timeout=FETCH_TIMEOUT_S)
        except requests.exceptions.RequestException as exc:
            warn(f"fetch failed for {url}: {exc}")
            continue
        if resp.status_code == 404:
            continue
        if resp.status_code != 200:
            warn(f"fetch failed for {url}: HTTP {resp.status_code}")
            continue
        return resp.text
    warn(f"could not fetch article content for: {result_url}")
    return None


def is_up(cfg: Config, session: requests.Session | None = None,
          timeout: float = 5) -> bool:
    """True when kiwix-serve answers at all (used by doctor/ensure).

    A refused connection returns at once; ``timeout`` bounds a server that
    accepts and then hangs.
    """
    get = (session or requests).get
    try:
        resp = get(f"{cfg.kiwix_url}/", timeout=timeout)
    except requests.exceptions.RequestException:
        return False
    return resp.status_code < 500


def zim_dir_listing(zim_dir: Path) -> list[str]:
    """Every .zim present on disk, sorted (for `infoforge zim list`)."""
    if not zim_dir.is_dir():
        return []
    return sorted(p.name for p in zim_dir.glob("*.zim"))
