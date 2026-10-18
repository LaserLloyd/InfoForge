"""Configuration loading and the process exit-code contract.

The real config lives OUTSIDE the repo — ``$XDG_CONFIG_HOME/infoforge/config.yaml``
(``~/.config/infoforge/config.yaml`` when XDG_CONFIG_HOME is unset) by default,
or wherever ``INFOFORGE_CONFIG`` / ``--config`` points.  Nothing here ever
falls back to a hardcoded host or model: a missing ``model_id`` is a fatal,
pointed error rather than a guess, because silently answering from the wrong
brain is worse than not answering at all.

Every key is declared once in :mod:`infoforge.settings_schema`; this module
validates against that schema.  Precedence per key: CLI flag > the
``INFOFORGE_<KEY>`` environment variable > the config file > the schema default.

The exit codes and the exception classes live in :mod:`infoforge.errors` (the
one catalog) and are re-exported here, because every other module already
imports them from this one.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import settings_schema as schema
from .errors import (  # noqa: F401  (re-exported: the historical import location)
    EXIT_CITATION,
    EXIT_CONFIG,
    EXIT_LLM,
    EXIT_NO_GROUNDING,
    EXIT_NOT_IN_SOURCES,
    EXIT_OK,
    ConfigError,
    InfoForgeError,
    LLMUnreachableError,
    NoGroundingError,
)
from .logging_ import warn

DEFAULT_CONFIG_PATH = Path("~/.config/infoforge/config.yaml")

#: ZIM filename prefixes of the Wikimedia family (content is CC BY-SA 4.0).
_WIKIMEDIA_PREFIXES = (
    "wikipedia", "wiktionary", "wikibooks", "wikiquote", "wikisource", "wikinews",
    "wikiversity", "wikivoyage", "wikispecies", "wikimedia",
)
LICENSE_WIKIMEDIA = "CC BY-SA 4.0"
LICENSE_UNKNOWN = "unknown"


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
    license: str = ""

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

    @property
    def license_label(self) -> str:
        """The licence string printed in attribution: configured, else a default.

        Wikimedia-family ZIMs (Wikipedia, Wiktionary, ...) are CC BY-SA 4.0;
        anything else is ``unknown`` unless the registry entry says otherwise.
        """
        if self.license:
            return self.license
        if self.file.lower().startswith(_WIKIMEDIA_PREFIXES):
            return LICENSE_WIKIMEDIA
        return LICENSE_UNKNOWN


@dataclass(frozen=True)
class Config:
    """A validated InfoForge configuration."""

    model_id: str
    base_url: str
    api_key: str = ""
    studioforge: bool = False
    fallbacks: tuple[Endpoint, ...] = ()
    kiwix_bind: str = "127.0.0.1"
    kiwix_port: int = 8090
    kiwix_url: str = ""
    kiwix_image_tag: str = schema.DEFAULT_KIWIX_IMAGE_TAG
    zim_dir: Path = field(default_factory=lambda: Path.home() / "zims")
    zims: tuple[Zim, ...] = ()
    max_articles: int = 5
    max_chars: int = 8000
    search_pool: int = 15
    timeout_s: int = 180
    hold_wait_s: int = 300
    log_questions: bool = False
    path: Path | None = None
    #: Where each key's effective value came from: cli | env | file | default | derived.
    sources: dict = field(default_factory=dict, compare=False, repr=False)

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

    def license_for_url(self, url: str) -> str:
        """Licence label of the ZIM a ``/content/<book>/...`` URL belongs to."""
        marker = "/content/"
        if marker in url:
            book = url.split(marker, 1)[1].split("/", 1)[0]
            for zim in self.zims:
                if zim.books_name == book:
                    return zim.license_label
        return LICENSE_UNKNOWN


def config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve which config file to read.

    ``--config`` > ``INFOFORGE_CONFIG`` > ``$XDG_CONFIG_HOME/infoforge/config.yaml``
    (an absolute XDG_CONFIG_HOME only, per the spec) > ``~/.config/infoforge/config.yaml``.
    The XDG step matches ``deploy/install.sh``, which seeds the file there.
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("INFOFORGE_CONFIG")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg and Path(xdg).is_absolute():
        return Path(xdg) / "infoforge" / "config.yaml"
    return DEFAULT_CONFIG_PATH.expanduser()


# Thin aliases kept for callers that used the private helpers before the schema module.
def _as_int(value, key: str, path: Path) -> int:
    return schema.as_int(value, key, str(path))


def _as_bool(value, key: str, path: Path) -> bool:
    return schema.as_bool(value, key, str(path))


# A ZIM filename must be a bare name: no separator of either kind, no control chars.
_BAD_FILENAME = re.compile(r"[\\/\x00-\x1f]")


def _parse_zims(raw, where: str) -> tuple[Zim, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{where}: zims must be a list of {{file, name, enabled}} entries")
    out: list[Zim] = []
    for i, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: zims[{i}] must be a mapping, got {type(entry).__name__}")
        file_name = str(entry.get("file") or "").strip()
        if not file_name:
            raise ConfigError(f"{where}: zims[{i}] is missing the required 'file' key")
        if _BAD_FILENAME.search(file_name) or file_name.startswith("."):
            # The name is interpolated into a container mount path, so keep it
            # a bare filename: no traversal, no absolute paths, no backslashes
            # (a Windows separator would escape zim_dir on that platform).
            raise ConfigError(
                f"{where}: zims[{i}].file must be a bare filename inside zim_dir, "
                f"got {file_name!r}"
            )
        out.append(
            Zim(
                file=file_name,
                name=str(entry.get("name") or file_name),
                # _as_bool, not bool(): the string "false" is truthy and used to
                # silently enable a ZIM the owner had switched off.
                enabled=schema.as_bool(entry.get("enabled", True), f"zims[{i}].enabled", where),
                license=str(entry.get("license") or "").strip(),
            )
        )
    return tuple(out)


def _parse_fallbacks(raw, where: str, default_key: str) -> tuple[Endpoint, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(
            f"{where}: fallbacks must be a list of {{model_id, base_url, api_key}} entries"
        )
    out: list[Endpoint] = []
    for i, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: fallbacks[{i}] must be a mapping")
        model_id = str(entry.get("model_id") or "").strip()
        base_url = str(entry.get("base_url") or "").strip()
        if not model_id or not base_url:
            raise ConfigError(
                f"{where}: fallbacks[{i}] needs both model_id and base_url"
            )
        api_key = entry.get("api_key")
        # An omitted api_key inherits the primary's; an explicit "" means none.
        key = default_key if api_key is None else str(api_key)
        out.append(Endpoint(model_id=model_id, base_url=base_url, api_key=key))
    return tuple(out)


def _missing_file_error(path: Path) -> ConfigError:
    return ConfigError(
        f"no config file at {path}\n"
        "  Create it by copying config.example.yaml from the InfoForge repo:\n"
        f"    mkdir -p {path.parent} && cp config.example.yaml {path}\n"
        "  then set model_id and base_url for your LLM endpoint.\n"
        "  (Point INFOFORGE_CONFIG somewhere else to use a different file, or set\n"
        "  INFOFORGE_MODEL_ID and INFOFORGE_BASE_URL to run without a file.)",
        code="config_missing",
    )


def _read_file(path: Path) -> dict | None:
    """The parsed mapping, or None when the file does not exist."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}", code="config_unreadable") from None
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"cannot parse {path} as YAML: {exc}",
                          code="config_unreadable") from None
    if loaded is None:
        raise ConfigError(f"{path} is empty — it must set at least model_id and base_url")
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a YAML mapping of key: value pairs")
    for key in loaded:
        if key not in schema.BY_KEY:
            warn(f"{path}: unknown key {key!r} ignored (see `infoforge config schema`)")
    return loaded


def load(explicit: str | os.PathLike[str] | None = None,
         overrides: dict[str, Any] | None = None,
         environ: dict[str, str] | None = None) -> Config:
    """Read, validate and return the config, or raise ConfigError with advice.

    ``overrides`` are CLI-flag values (highest precedence); ``environ`` lets a
    test pass its own environment instead of ``os.environ``.
    """
    path = config_path(explicit)
    env = os.environ if environ is None else environ
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}

    from_file = _read_file(path)
    file_found = from_file is not None
    parsed: dict = from_file or {}

    # key -> (raw value, source, label used in error messages)
    values: dict[str, tuple[Any, str, str]] = {}
    for setting in schema.SETTINGS:
        key = setting.key
        env_raw = schema.env_value(setting, env)
        if key in overrides:
            values[key] = (overrides[key], "cli", f"--{key.replace('_', '-')}")
        elif env_raw is not None:
            where = f"${setting.env}"
            raw = schema.parse_env_list(setting, env_raw, where) if setting.type == "list" \
                else env_raw
            values[key] = (raw, "env", where)
        elif parsed.get(key) is not None:
            values[key] = (parsed[key], "file", str(path))
        else:
            values[key] = (None, "default", str(path))
    sources = {key: src for key, (_, src, _) in values.items()}

    # --- required keys, with the pointed messages 2.0 had
    model_raw, _, model_where = values["model_id"]
    model_id = str(model_raw or "").strip()
    base_raw, _, base_where = values["base_url"]
    base_url = str(base_raw or "").strip()
    if not file_found and not (model_id and base_url):
        raise _missing_file_error(path)
    if not model_id:
        raise ConfigError(
            f"{model_where}: model_id is not set — InfoForge refuses to guess a model"
        )
    if not base_url:
        raise ConfigError(
            f"{base_where}: base_url is not set — point it at an OpenAI-compatible /v1 endpoint"
        )
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"{base_where}: base_url must start with http:// or https://, got {base_url!r}"
        )

    def scalar(key: str):
        setting = schema.BY_KEY[key]
        raw, src, where = values[key]
        if src == "default":
            if setting.legacy_default is not schema.NO_LEGACY_DEFAULT and file_found:
                return setting.legacy_default
            return schema.coerce(setting, setting.default, where)
        return schema.coerce(setting, raw, where)

    api_key = str(values["api_key"][0] or "")
    kiwix_bind = scalar("kiwix_bind") or "127.0.0.1"
    kiwix_port = scalar("kiwix_port")
    kiwix_url = scalar("kiwix_url")
    if not kiwix_url:
        # Derived, so a bind/port change does not need two edits kept in sync.
        host = "127.0.0.1" if kiwix_bind in ("0.0.0.0", "", "::") else kiwix_bind
        kiwix_url = f"http://{host}:{kiwix_port}"
        sources["kiwix_url"] = "derived"

    return Config(
        model_id=model_id,
        base_url=base_url,
        api_key=api_key,
        studioforge=scalar("studioforge"),
        fallbacks=_parse_fallbacks(values["fallbacks"][0], values["fallbacks"][2], api_key),
        kiwix_bind=kiwix_bind,
        kiwix_port=kiwix_port,
        kiwix_url=kiwix_url.rstrip("/"),
        kiwix_image_tag=scalar("kiwix_image_tag") or schema.DEFAULT_KIWIX_IMAGE_TAG,
        zim_dir=scalar("zim_dir"),
        zims=_parse_zims(values["zims"][0], values["zims"][2]),
        max_articles=scalar("max_articles"),
        max_chars=scalar("max_chars"),
        search_pool=scalar("search_pool"),
        timeout_s=scalar("timeout_s"),
        hold_wait_s=scalar("hold_wait_s"),
        log_questions=scalar("log_questions"),
        path=path if file_found else None,
        sources=sources,
    )


def show(cfg: Config) -> dict:
    """The effective configuration with each value's source; secrets redacted.

    This is what ``infoforge config show --json`` prints.
    """
    rows = []
    for setting in schema.SETTINGS:
        value: Any = getattr(cfg, setting.key)
        if setting.key == "fallbacks":
            value = [{"model_id": e.model_id, "base_url": e.base_url,
                      "api_key": "***" if e.api_key else ""} for e in cfg.fallbacks]
        elif setting.key == "zims":
            value = [{"file": z.file, "name": z.name, "enabled": z.enabled,
                      "license": z.license_label} for z in cfg.zims]
        elif setting.secret:
            value = "***" if value else ""
        elif isinstance(value, Path):
            value = str(value)
        rows.append({"key": setting.key, "value": value,
                     "source": cfg.sources.get(setting.key, "default"),
                     "env": setting.env})
    return {"config_file": str(cfg.path) if cfg.path else None,
            "schema_version": schema.SCHEMA_VERSION, "settings": rows}
