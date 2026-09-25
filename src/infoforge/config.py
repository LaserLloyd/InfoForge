"""Configuration loading and the process exit-code contract.

The real config lives OUTSIDE the repo — ``~/.config/infoforge/config.yaml`` by
default, or wherever ``INFOFORGE_CONFIG`` points.  Nothing here ever falls back
to a hardcoded host or model: a missing ``model_id`` is a fatal, pointed error
rather than a guess, because silently answering from the wrong brain is worse
than not answering at all.

The exit codes live here too.  Every other module needs them and config errors
are the first thing that can raise, so this is the one module everyone already
imports.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# ------------------------------------------------------------- exit contract
EXIT_OK = 0
EXIT_CONFIG = 2       # bad config or bad usage
EXIT_NO_GROUNDING = 3  # retrieval found nothing to ground on
EXIT_CITATION = 4     # --strict and the answer cited none of the sources
EXIT_LLM = 5          # every LLM in the chain was unreachable
EXIT_NOT_IN_SOURCES = 6  # the model said the retrieved sources lack the answer

DEFAULT_CONFIG_PATH = Path("~/.config/infoforge/config.yaml")


class InfoForgeError(Exception):
    """Base error carrying the exit code the CLI should return."""

    exit_code = EXIT_CONFIG


class ConfigError(InfoForgeError):
    """Config file missing, unparseable, or missing a required key."""

    exit_code = EXIT_CONFIG


class NoGroundingError(InfoForgeError):
    """Retrieval produced no usable source text."""

    exit_code = EXIT_NO_GROUNDING


class LLMUnreachableError(InfoForgeError):
    """Primary and every fallback failed at the transport level."""

    exit_code = EXIT_LLM


@dataclass(frozen=True)
class Endpoint:
    """One OpenAI-compatible chat endpoint (the primary or a fallback)."""

    model_id: str
    base_url: str
    api_key: str = ""

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/models"


@dataclass(frozen=True)
class Zim:
    """One archive in the enabled-ZIM registry (config ``zims:``)."""

    file: str
    name: str = ""
    enabled: bool = True

    @property
    def books_name(self) -> str:
        """The name kiwix-serve reports for this book.

        kiwix-serve derives a book's name from the ZIM filename without the
        ``.zim`` extension, and that is exactly what the search endpoint's
        ``books.name`` filter expects (see /search/searchdescription.xml).
        Without the filter, kiwix-serve answers HTTP 400 as soon as two or
        more books in different languages are loaded.
        """
        return self.file[:-4] if self.file.endswith(".zim") else self.file


@dataclass(frozen=True)
class Config:
    """A validated InfoForge configuration."""

    model_id: str
    base_url: str
    api_key: str = ""
    studioforge: bool = False
    fallbacks: tuple[Endpoint, ...] = ()
    kiwix_bind: str = "127.0.0.1"
    kiwix_port: int = 8080
    kiwix_url: str = ""
    zim_dir: Path = field(default_factory=lambda: Path.home() / "zims")
    zims: tuple[Zim, ...] = ()
    max_articles: int = 5
    max_chars: int = 8000
    search_pool: int = 15
    timeout_s: int = 180
    hold_wait_s: int = 300
    path: Path | None = None

    @property
    def primary(self) -> Endpoint:
        return Endpoint(self.model_id, self.base_url, self.api_key)

    @property
    def endpoints(self) -> tuple[Endpoint, ...]:
        """The full LLM chain: primary first, then each fallback in order."""
        return (self.primary,) + tuple(self.fallbacks)

    @property
    def enabled_zims(self) -> tuple[Zim, ...]:
        return tuple(z for z in self.zims if z.enabled)


def config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve which config file to read: explicit > INFOFORGE_CONFIG > default."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("INFOFORGE_CONFIG")
    if env:
        return Path(env).expanduser()
    return DEFAULT_CONFIG_PATH.expanduser()


def _as_int(value, key: str, path: Path) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{path}: {key} must be an integer, got {value!r}") from None


def _as_bool(value, key: str, path: Path) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no"):
        return value.strip().lower() in ("true", "yes")
    raise ConfigError(f"{path}: {key} must be true or false, got {value!r}")


def _parse_zims(raw, path: Path) -> tuple[Zim, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{path}: zims must be a list of {{file, name, enabled}} entries")
    out: list[Zim] = []
    for i, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: zims[{i}] must be a mapping, got {type(entry).__name__}")
        file_name = str(entry.get("file") or "").strip()
        if not file_name:
            raise ConfigError(f"{path}: zims[{i}] is missing the required 'file' key")
        if "/" in file_name or file_name.startswith("."):
            # The name is interpolated into a container mount path, so keep it
            # a bare filename — no traversal, no absolute paths.
            raise ConfigError(
                f"{path}: zims[{i}].file must be a bare filename inside zim_dir, got {file_name!r}"
            )
        out.append(
            Zim(
                file=file_name,
                name=str(entry.get("name") or file_name),
                enabled=bool(entry.get("enabled", True)),
            )
        )
    return tuple(out)


def _parse_fallbacks(raw, path: Path, default_key: str) -> tuple[Endpoint, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(
            f"{path}: fallbacks must be a list of {{model_id, base_url, api_key}} entries"
        )
    out: list[Endpoint] = []
    for i, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: fallbacks[{i}] must be a mapping")
        model_id = str(entry.get("model_id") or "").strip()
        base_url = str(entry.get("base_url") or "").strip()
        if not model_id or not base_url:
            raise ConfigError(
                f"{path}: fallbacks[{i}] needs both model_id and base_url"
            )
        api_key = entry.get("api_key")
        # An omitted api_key inherits the primary's; an explicit "" means none.
        key = default_key if api_key is None else str(api_key)
        out.append(Endpoint(model_id=model_id, base_url=base_url, api_key=key))
    return tuple(out)


def load(explicit: str | os.PathLike[str] | None = None) -> Config:
    """Read, validate and return the config, or raise ConfigError with advice."""
    path = config_path(explicit)

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(
            f"no config file at {path}\n"
            "  Create it by copying config.example.yaml from the InfoForge repo:\n"
            f"    mkdir -p {path.parent} && cp config.example.yaml {path}\n"
            "  then set model_id and base_url for your LLM endpoint.\n"
            "  (Point INFOFORGE_CONFIG somewhere else to use a different file.)"
        ) from None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None

    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"cannot parse {path} as YAML: {exc}") from None

    if parsed is None:
        raise ConfigError(f"{path} is empty — it must set at least model_id and base_url")
    if not isinstance(parsed, dict):
        raise ConfigError(f"{path} must contain a YAML mapping of key: value pairs")

    model_id = str(parsed.get("model_id") or "").strip()
    if not model_id:
        raise ConfigError(
            f"{path}: model_id is not set — InfoForge refuses to guess a model"
        )
    base_url = str(parsed.get("base_url") or "").strip()
    if not base_url:
        raise ConfigError(
            f"{path}: base_url is not set — point it at an OpenAI-compatible /v1 endpoint"
        )
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"{path}: base_url must start with http:// or https://, got {base_url!r}")

    api_key = str(parsed.get("api_key") or "")

    kiwix_bind = str(parsed.get("kiwix_bind") or "127.0.0.1").strip()
    kiwix_port = _as_int(parsed.get("kiwix_port", 8080), "kiwix_port", path)
    if not 1 <= kiwix_port <= 65535:
        raise ConfigError(f"{path}: kiwix_port must be 1-65535, got {kiwix_port}")

    kiwix_url = str(parsed.get("kiwix_url") or "").strip()
    if not kiwix_url:
        # Derived, so a bind/port change does not need two edits kept in sync.
        host = "127.0.0.1" if kiwix_bind in ("0.0.0.0", "", "::") else kiwix_bind
        kiwix_url = f"http://{host}:{kiwix_port}"

    zim_dir = Path(str(parsed.get("zim_dir") or "~/zims")).expanduser()
    zims = _parse_zims(parsed.get("zims"), path)

    cfg = Config(
        model_id=model_id,
        base_url=base_url,
        api_key=api_key,
        studioforge=_as_bool(parsed.get("studioforge", False), "studioforge", path),
        fallbacks=_parse_fallbacks(parsed.get("fallbacks"), path, api_key),
        kiwix_bind=kiwix_bind,
        kiwix_port=kiwix_port,
        kiwix_url=kiwix_url.rstrip("/"),
        zim_dir=zim_dir,
        zims=zims,
        max_articles=_as_int(parsed.get("max_articles", 5), "max_articles", path),
        max_chars=_as_int(parsed.get("max_chars", 8000), "max_chars", path),
        search_pool=_as_int(parsed.get("search_pool", 15), "search_pool", path),
        timeout_s=_as_int(parsed.get("timeout_s", 180), "timeout_s", path),
        hold_wait_s=_as_int(parsed.get("hold_wait_s", 300), "hold_wait_s", path),
        path=path,
    )

    for key in ("max_articles", "max_chars", "search_pool", "timeout_s"):
        if getattr(cfg, key) < 1:
            raise ConfigError(f"{path}: {key} must be >= 1, got {getattr(cfg, key)}")
    # 0 is meaningful here: it disables the busy-wait and restores fail-fast.
    if cfg.hold_wait_s < 0:
        raise ConfigError(f"{path}: hold_wait_s must be >= 0, got {cfg.hold_wait_s}")

    return cfg
