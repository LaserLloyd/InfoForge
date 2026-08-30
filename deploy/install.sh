#!/usr/bin/env bash
#
# InfoForge installer — idempotent. Safe to re-run after every `git pull`.
#
#   deploy/install.sh
#
# What it does, in order:
#   1. installs the `infoforge` console script with uv (--reinstall, so a
#      re-run always lands the current tree);
#   2. seeds ~/.config/infoforge/config.yaml from config.example.yaml, but ONLY
#      if it does not exist — your real config is never overwritten. When the
#      predecessor's config (~/localwiki/config.yaml) is present, its LLM
#      settings are migrated into that first seed;
#   3. installs + enables the user systemd unit (this one IS overwritten — the
#      repo is the source of truth for the unit);
#   4. removes the predecessor's `localwiki-kiwix` container, so the two do not
#      fight over the same port;
#   5. starts the service and runs `infoforge doctor`, exiting with doctor's
#      status.
#
# NOTE ON FAILURE HANDLING: there is deliberately no `|| true` anywhere in this
# script. A step that cannot do its job says so and stops. The only place a
# non-zero status is captured rather than fatal is `infoforge doctor` at the
# very end, whose status is the script's status.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/infoforge"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
EXAMPLE_FILE="$REPO_ROOT/config.example.yaml"
UNIT_SRC="$REPO_ROOT/deploy/infoforge.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_NAME="infoforge.service"
BIN="$HOME/.local/bin/infoforge"

# The predecessor. Both are read-only to this script: we migrate settings out
# of the old config and remove the old container, and touch nothing else.
LEGACY_CONFIG="$HOME/localwiki/config.yaml"
LEGACY_CONTAINER="localwiki-kiwix"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31minstall.sh: %s\033[0m\n' "$*" >&2; exit 1; }

need() {
    command -v "$1" >/dev/null 2>&1 \
        || die "$1 is required but not on PATH. $2"
}

need uv      "Install it from https://docs.astral.sh/uv/ and re-run."
need python3 "Install Python 3.12+ and re-run."
need podman  "InfoForge serves ZIMs from a rootless podman container; there is no other backend."
need systemctl "This installer wires a systemd --user unit."

# ---------------------------------------------------------------- 1. package
say "Installing the infoforge CLI (uv tool install --reinstall)"
uv tool install --reinstall "$REPO_ROOT"
[ -x "$BIN" ] || die "uv reported success but $BIN is not executable. \
Is ~/.local/bin on uv's tool bin path? Try: uv tool update-shell"
info "$BIN"

# ----------------------------------------------------------------- 2. config
say "Configuration"
mkdir -p "$CONFIG_DIR"
if [ -e "$CONFIG_FILE" ]; then
    info "$CONFIG_FILE exists — left untouched."
else
    [ -f "$EXAMPLE_FILE" ] || die "no $EXAMPLE_FILE to seed from."
    # The seed is written by python3 so the migration can parse real YAML when
    # PyYAML is importable and fall back to a tolerant line parser when it is
    # not. Both paths read the OLD file at runtime — no host, URL or model name
    # is baked into this script.
    INFOFORGE_EXAMPLE="$EXAMPLE_FILE" \
    INFOFORGE_SEED="$CONFIG_FILE" \
    INFOFORGE_LEGACY="$LEGACY_CONFIG" \
    python3 -c '
import os, re, sys
from pathlib import Path

example = Path(os.environ["INFOFORGE_EXAMPLE"])
seed    = Path(os.environ["INFOFORGE_SEED"])
legacy  = Path(os.environ["INFOFORGE_LEGACY"])

MIGRATE = ["model_id", "base_url", "api_key", "max_articles", "max_chars"]

def tolerant(text):
    """Top-level `key: value` pairs, quotes stripped. No nesting, on purpose:
    we only ever migrate five scalars, and a hand-rolled parser that tried to
    do more would be a second YAML implementation to get wrong."""
    out = {}
    for line in text.splitlines():
        if not line or line[0] in " \t#-":
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        val = val.split(" #")[0].strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"\x27":
            out[key] = val[1:-1]
            continue
        # Unquoted scalars keep their YAML type. Without this, max_articles
        # migrated out of an unquoted `max_articles: 5` came back as the STRING
        # "5" and the seed said `max_articles: "5"` — valid YAML, wrong type,
        # and a config error the user did not cause.
        if re.fullmatch(r"-?\d+", val):
            out[key] = int(val)
        elif val.lower() in ("true", "false"):
            out[key] = val.lower() == "true"
        else:
            out[key] = val
    return out

old = {}
if legacy.is_file():
    text = legacy.read_text(encoding="utf-8")
    try:
        import yaml
        loaded = yaml.safe_load(text)
        old = loaded if isinstance(loaded, dict) else {}
    except ImportError:
        old = tolerant(text)
    old = {k: v for k, v in old.items() if k in MIGRATE and v is not None}

lines = example.read_text(encoding="utf-8").splitlines()

def render(key, value):
    if isinstance(value, bool):
        return f"{key}: {str(value).lower()}"
    if isinstance(value, (int, float)):
        return f"{key}: {value}"
    return f"{key}: \"{value}\""

updates = dict(old)
if old:
    base = str(old.get("base_url", ""))
    host = re.sub(r"^[a-z]+://", "", base).split("/")[0].split(":")[0]
    # A remote OpenAI-compatible endpoint on this box means StudioForge, which
    # wants a scheduling "priority" field on chat bodies. A loopback endpoint
    # (LM Studio, llama.cpp) does not.
    updates["studioforge"] = host not in ("127.0.0.1", "localhost", "::1", "")

seen, out = set(), []
for line in lines:
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:", line)
    key = m.group(1) if m else None
    if key in updates:
        out.append(render(key, updates[key]))
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(render(key, value))

seed.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
seed.chmod(0o600)

if old:
    print("    migrated from the previous install: " + ", ".join(sorted(old)))
    print("    studioforge: %s (from the migrated base_url)"
          % str(updates["studioforge"]).lower())
else:
    print("    seeded from config.example.yaml (no previous config found)")
'
    info "wrote $CONFIG_FILE (mode 600) — review it before trusting doctor."
fi

# ------------------------------------------------------------------- 3. unit
say "systemd --user unit"
mkdir -p "$UNIT_DIR"
install -m 644 "$UNIT_SRC" "$UNIT_DIR/$UNIT_NAME"
info "installed $UNIT_DIR/$UNIT_NAME (overwrote any previous copy)"
systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME"
info "enabled (WantedBy=default.target)"

# ------------------------------------------------- 4. retire the predecessor
say "Retiring the previous kiwix container"
existing="$(podman ps -a --filter "name=^${LEGACY_CONTAINER}\$" --format '{{.Names}}')"
if [ -n "$existing" ]; then
    podman stop "$LEGACY_CONTAINER"
    podman rm "$LEGACY_CONTAINER"
    info "stopped and removed '$LEGACY_CONTAINER' (it held the same port)"
else
    info "no '$LEGACY_CONTAINER' container present — nothing to retire"
fi

# ------------------------------------------------------------------ 5. start
say "Starting InfoForge"
"$BIN" serve start

say "Health check (infoforge doctor)"
doctor_status=0
"$BIN" doctor || doctor_status=$?

printf '\n'
if [ "$doctor_status" -eq 0 ]; then
    printf '\033[32mInfoForge is installed and healthy.\033[0m\n'
    printf 'Try:  infoforge ask "What is the capital of France?"\n'
else
    printf '\033[31mInfoForge is installed but doctor exited %s — read its table above.\033[0m\n' \
        "$doctor_status"
    printf 'Nothing was rolled back; fix the failing row and re-run `infoforge doctor`.\n'
fi
exit "$doctor_status"
