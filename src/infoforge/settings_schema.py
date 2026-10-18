"""The declarative settings schema: the single source of truth for every config key.

Everything that needs to know "what settings exist" is generated from
:data:`SETTINGS`:

* :func:`infoforge.config.load` validates and coerces each key against it;
* ``INFOFORGE_<KEY>`` environment overrides come from :attr:`Setting.env`;
* ``infoforge config schema --json`` prints :func:`json_schema`;
* ``docs/reference/config.md`` and ``config.example.yaml``'s key list are
  checked against it (``scripts/gen_docs.py`` + a drift test).

Precedence for a key, highest first: CLI flag > ``INFOFORGE_<KEY>`` environment
variable > config file > default.  Only ``max_articles`` has a CLI flag today.

Add a key by adding one :class:`Setting` here; there is nowhere else to
register it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

SCHEMA_VERSION = 1

#: Default kiwix image tag.  Pinned (not ``latest``) so a pull cannot silently
#: change the server under a working install; bump it deliberately and pull
#: the image yourself: ``podman pull ghcr.io/kiwix/kiwix-serve:<tag>``.
DEFAULT_KIWIX_IMAGE_TAG = "3.8.2"

#: Marker: "no legacy default" (the schema default always applies).
NO_LEGACY_DEFAULT: Any = object()


@dataclass(frozen=True)
class Setting:
    key: str
    type: str                 # string | integer | boolean | path | url | list
    default: Any
    description: str
    level: str = "advanced"   # basic | advanced
    group: str = "general"
    required: bool = False
    secret: bool = False
    minimum: int | None = None
    maximum: int | None = None
    #: Applied instead of ``default`` when a config *file* exists but omits the
    #: key (so upgrading never changes the value of an existing install).
    legacy_default: Any = NO_LEGACY_DEFAULT
    derived: bool = False     # default is computed from other keys

    @property
    def env(self) -> str:
        return f"INFOFORGE_{self.key.upper()}"

    def range_text(self) -> str:
        if self.minimum is not None and self.maximum is not None:
            return f"{self.minimum}-{self.maximum}"
        if self.minimum is not None:
            return f">= {self.minimum}"
        return ""


SETTINGS: tuple[Setting, ...] = (
    Setting("model_id", "string", None, "Model name the LLM server should use. Required; "
            "InfoForge refuses to guess a model.", "basic", "llm", required=True),
    Setting("base_url", "url", None, "OpenAI-compatible API root; InfoForge POSTs to "
            "`{base_url}/chat/completions`. Must start with http:// or https://.",
            "basic", "llm", required=True),
    Setting("api_key", "string", "", "Bearer token for the LLM endpoint. The Authorization "
            "header is omitted entirely when empty. Prefer the INFOFORGE_API_KEY "
            "environment variable over plaintext in the file.", "basic", "llm", secret=True),
    Setting("studioforge", "boolean", False, "Set true when base_url is a StudioForge server: "
            "chat bodies then carry its scheduling `priority` field and client header.",
            "advanced", "llm"),
    Setting("fallbacks", "list", [], "Endpoints `{model_id, base_url, api_key}` tried in order "
            "when the primary is unreachable (connection error / 5xx). An omitted api_key "
            "inherits the primary's. Via the environment this is a JSON list.",
            "advanced", "llm"),
    Setting("kiwix_bind", "string", "127.0.0.1", "Host address the kiwix container publishes "
            "on. Keep it on loopback unless you mean to serve the whole library to the "
            "network: kiwix-serve has no authentication.", "advanced", "kiwix"),
    Setting("kiwix_port", "integer", 8090, "Host port the kiwix container publishes on. "
            "Fresh configs use 8090 because 8080 collides with the StudioForge panel on "
            "machines that run both; a config file that omits this key keeps 8080.",
            "advanced", "kiwix", minimum=1, maximum=65535, legacy_default=8080),
    Setting("kiwix_url", "url", "", "Base URL of kiwix-serve. Derived from "
            "kiwix_bind/kiwix_port when unset. Set it (and do not use `serve`) to use "
            "a kiwix-serve you run yourself, on any OS, without podman.",
            "basic", "kiwix", derived=True),
    Setting("kiwix_image_tag", "string", DEFAULT_KIWIX_IMAGE_TAG, "Tag of the "
            "ghcr.io/kiwix/kiwix-serve image the container is created from. Pinned on "
            "purpose; pull it yourself with `podman pull ghcr.io/kiwix/kiwix-serve:<tag>`.",
            "advanced", "kiwix"),
    Setting("zim_dir", "path", "~/zims", "Directory holding the .zim archives; mounted "
            "into the container. Never inside the repo.", "basic", "kiwix"),
    Setting("zims", "list", [], "The ZIM registry: entries `{file, name, enabled, "
            "license}`. `file` is a bare filename inside zim_dir; only enabled files "
            "that exist are served. `license` (optional) is the licence string printed in "
            "`ask` attribution (default: CC BY-SA 4.0 for Wikipedia-family ZIMs, "
            "`unknown` otherwise). Via the environment this is a JSON list.",
            "basic", "kiwix"),
    Setting("max_articles", "integer", 5, "How many reranked articles are fetched and fed "
            "to the model (CLI: --max-articles).", "basic", "retrieval", minimum=1),
    Setting("max_chars", "integer", 8000, "Characters of article text per source after "
            "keyword-aware truncation.", "advanced", "retrieval", minimum=1),
    Setting("search_pool", "integer", 15, "Full-text hits pulled per ZIM before reranking.",
            "advanced", "retrieval", minimum=1),
    Setting("timeout_s", "integer", 180, "Timeout in seconds for LLM requests only. The "
            "kiwix HTTP timeouts are fixed (20 s search, 15 s suggest, 30 s fetch).",
            "advanced", "llm", minimum=1),
    Setting("hold_wait_s", "integer", 300, "Seconds to wait out an endpoint that answers "
            "busy (HTTP 503/429, e.g. StudioForge priority_hold) before moving to the "
            "next fallback. 0 fails straight through.", "advanced", "llm", minimum=0),
    Setting("log_questions", "boolean", False, "Store question text in the event log "
            "(~/.local/state/infoforge/log.jsonl). Off by default: only a hash and the "
            "length are stored.", "advanced", "privacy"),
)

BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}


# ------------------------------------------------------------------- coercion

def _fail(where: str, key: str, problem: str) -> ConfigError:
    return ConfigError(f"{where}: {key} {problem}", code="config_invalid")


def as_int(value: Any, key: str, where: str) -> int:
    if isinstance(value, bool):
        raise _fail(where, key, f"must be an integer, got {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise _fail(where, key, f"must be an integer, got {value!r}") from None


def as_bool(value: Any, key: str, where: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no"):
        return value.strip().lower() in ("true", "yes")
    raise _fail(where, key, f"must be true or false, got {value!r}")


def coerce(setting: Setting, raw: Any, where: str) -> Any:
    """Validate and convert one scalar value against its schema row.

    ``list`` settings are parsed by their own functions in :mod:`config`
    (they have nested structure); this handles every scalar type.
    """
    key = setting.key
    if setting.type == "integer":
        number = as_int(raw, key, where)
        too_small = setting.minimum is not None and number < setting.minimum
        too_big = setting.maximum is not None and number > setting.maximum
        if too_small or too_big:
            raise _fail(where, key, f"must be {setting.range_text()}, got {number}")
        return number
    if setting.type == "boolean":
        return as_bool(raw, key, where)
    text = "" if raw is None else str(raw).strip()
    if setting.type == "url":
        if text and not text.startswith(("http://", "https://")):
            raise _fail(where, key, f"must start with http:// or https://, got {text!r}")
        return text
    if setting.type == "path":
        return Path(text or str(setting.default)).expanduser()
    return text


def parse_env_list(setting: Setting, raw: str, where: str) -> Any:
    """A list-valued setting from the environment is a JSON document."""
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise _fail(where, setting.key, f"must be a JSON list in the environment: {exc}") from None
    return value


def env_value(setting: Setting, environ: dict[str, str] | None = None) -> str | None:
    """The raw ``INFOFORGE_<KEY>`` value, or None when unset (empty counts as unset)."""
    value = (os.environ if environ is None else environ).get(setting.env)
    return value if value not in (None, "") else None


# --------------------------------------------------------------------- export

def _default_for_display(setting: Setting) -> Any:
    if setting.default is None:
        return None
    return list(setting.default) if isinstance(setting.default, list) else setting.default


def schema_rows() -> list[dict]:
    """One plain dict per setting (used by ``config schema`` and the docs)."""
    rows = []
    for s in SETTINGS:
        row: dict[str, Any] = {
            "key": s.key,
            "type": s.type,
            "default": _default_for_display(s),
            "required": s.required,
            "level": s.level,
            "group": s.group,
            "env": s.env,
            "secret": s.secret,
            "description": s.description,
        }
        if s.minimum is not None:
            row["minimum"] = s.minimum
        if s.maximum is not None:
            row["maximum"] = s.maximum
        if s.legacy_default is not NO_LEGACY_DEFAULT:
            row["legacy_default"] = s.legacy_default
        if s.derived:
            row["derived"] = True
        rows.append(row)
    return rows


_JSON_TYPES = {"string": "string", "url": "string", "path": "string",
               "integer": "integer", "boolean": "boolean", "list": "array"}


def json_schema() -> dict:
    """A JSON Schema (draft 2020-12) for the config file, plus InfoForge extras."""
    properties: dict[str, dict] = {}
    for s in SETTINGS:
        prop: dict[str, Any] = {"type": _JSON_TYPES[s.type], "description": s.description,
                                "x-env": s.env, "x-level": s.level, "x-group": s.group}
        if s.default is not None:
            prop["default"] = _default_for_display(s)
        if s.minimum is not None:
            prop["minimum"] = s.minimum
        if s.maximum is not None:
            prop["maximum"] = s.maximum
        if s.secret:
            prop["writeOnly"] = True
        if s.legacy_default is not NO_LEGACY_DEFAULT:
            prop["x-legacy-default"] = s.legacy_default
        properties[s.key] = prop
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "InfoForge config",
        "x-schema-version": SCHEMA_VERSION,
        "type": "object",
        "required": [s.key for s in SETTINGS if s.required],
        "properties": properties,
        "additionalProperties": True,
    }
