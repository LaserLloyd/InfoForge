#!/usr/bin/env bash
#
# InfoForge installer — idempotent. Safe to re-run after every `git pull`.
#
#   deploy/install.sh                    install / upgrade
#   deploy/install.sh --pull             ... and pull the pinned kiwix-serve image first
#   deploy/install.sh --uninstall        remove the service, container and CLI (keeps your data)
#   deploy/install.sh --migrate-localwiki   OPTIONAL: adopt the predecessor's settings (see the LEGACY block)
#
# What the install does, in order:
#   1. installs the `infoforge` console script with uv (--reinstall, so a
#      re-run always lands the current tree);
#   2. seeds $XDG_CONFIG_HOME/infoforge/config.yaml (~/.config/infoforge/...)
#      from config.example.yaml, but ONLY if it does not exist — your real
#      config is never overwritten;
#   3. installs + enables the user systemd unit (this one IS overwritten — the
#      repo is the source of truth for the unit);
#   4. starts the service and runs `infoforge doctor`, exiting with doctor's
#      status.
#
# Linux only: it needs podman and systemd --user. On any other OS run your own
# kiwix-serve, set kiwix_url in the config and skip this script (see the README,
# "Platform support").
#
# NOTE ON FAILURE HANDLING: there is deliberately no `|| true` in the install
# path. A step that cannot do its job says so and stops. The only place a
# non-zero status is captured rather than fatal is `infoforge doctor` at the
# very end, whose status is the script's status. (--uninstall is the exception:
# removing something that is already gone is success, so its steps tolerate
# "not found".)

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_ROOT="${XDG_CONFIG_HOME:-$HOME/.config}"
CONFIG_DIR="$CONFIG_ROOT/infoforge"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
EXAMPLE_FILE="$REPO_ROOT/config.example.yaml"
UNIT_SRC="$REPO_ROOT/deploy/infoforge.service"
UNIT_DIR="$CONFIG_ROOT/systemd/user"
UNIT_NAME="infoforge.service"
BIN="$HOME/.local/bin/infoforge"
CONTAINER="infoforge-kiwix"
IMAGE_REPO="ghcr.io/kiwix/kiwix-serve"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31minstall.sh: %s\033[0m\n' "$*" >&2; exit 1; }

need() {
    command -v "$1" >/dev/null 2>&1 \
        || die "$1 is required but not on PATH. $2"
}

usage() {
    sed -n '3,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# =============================================================================
# OPTIONAL, LEGACY: adopting the predecessor (the unpackaged ~/localwiki tree).
#
# Not part of a normal install. Runs only with --migrate-localwiki, and only
# ever (a) copies five scalar LLM settings out of ~/localwiki/config.yaml into
# a config this run has JUST seeded, and (b) stops and removes the predecessor's
# own 'localwiki-kiwix' container, which would otherwise hold the same port.
# It touches nothing else and is safe to delete along with the two calls below.
# =============================================================================
LEGACY_CONFIG="$HOME/localwiki/config.yaml"
LEGACY_CONTAINER="localwiki-kiwix"

legacy_migrate_settings() {
    [ -f "$LEGACY_CONFIG" ] || { info "no $LEGACY_CONFIG — nothing to migrate"; return 0; }
    # Reads the OLD file at runtime; no host, URL or model name is baked in.
    # PyYAML when importable, else a tolerant line parser.
    INFOFORGE_EXAMPLE="$CONFIG_FILE" \
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
}

legacy_retire_container() {
    local existing
    existing="$(podman ps -a --filter "name=^${LEGACY_CONTAINER}\$" --format '{{.Names}}')"
    if [ -n "$existing" ]; then
        podman stop "$LEGACY_CONTAINER"
        podman rm "$LEGACY_CONTAINER"
        info "stopped and removed '$LEGACY_CONTAINER' (it held the same port)"
    else
        info "no '$LEGACY_CONTAINER' container present — nothing to retire"
    fi
}
# ===================== end of the optional legacy block ======================

MODE=install
PULL=0
MIGRATE_LEGACY=0
for arg in "$@"; do
    case "$arg" in
        --uninstall)         MODE=uninstall ;;
        --pull)              PULL=1 ;;
        --migrate-localwiki) MIGRATE_LEGACY=1 ;;
        -h|--help)           usage; exit 0 ;;
        *) die "unknown option: $arg (try --help)" ;;
    esac
done

# --------------------------------------------------------------- uninstall
if [ "$MODE" = uninstall ]; then
    need systemctl "This installer wires a systemd --user unit."
    say "Removing the systemd --user unit"
    if [ -e "$UNIT_DIR/$UNIT_NAME" ]; then
        systemctl --user disable --now "$UNIT_NAME" 2>/dev/null \
            && info "stopped and disabled $UNIT_NAME" \
            || info "$UNIT_NAME was not enabled/running"
        rm -f "$UNIT_DIR/$UNIT_NAME"
        systemctl --user daemon-reload
        info "removed $UNIT_DIR/$UNIT_NAME"
    else
        info "no unit at $UNIT_DIR/$UNIT_NAME — nothing to remove"
    fi

    say "Removing the kiwix container"
    if command -v podman >/dev/null 2>&1 \
            && podman container exists "$CONTAINER" 2>/dev/null; then
        podman rm -f "$CONTAINER" >/dev/null
        info "removed container $CONTAINER"
    else
        info "no '$CONTAINER' container — nothing to remove"
    fi

    say "Removing the infoforge CLI"
    if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q '^infoforge '; then
        uv tool uninstall infoforge
    else
        info "infoforge is not installed as a uv tool — nothing to remove"
    fi

    say "Kept (delete by hand if you want them gone)"
    info "config:  $CONFIG_DIR"
    info "ZIMs:    your zim_dir (default ~/zims)"
    info "logs:    ${XDG_STATE_HOME:-$HOME/.local/state}/infoforge"
    info "image:   podman rmi $IMAGE_REPO:<tag>   (frees the pulled kiwix-serve image)"
    printf '\n\033[32mInfoForge is uninstalled; your data was not touched.\033[0m\n'
    exit 0
fi

need uv      "Install it from https://docs.astral.sh/uv/ and re-run."
need python3 "Install Python 3.12+ and re-run."
need podman  "InfoForge serves ZIMs from a rootless podman container. Without podman, run your own kiwix-serve, set kiwix_url and skip this script."
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
    install -m 600 "$EXAMPLE_FILE" "$CONFIG_FILE"
    info "seeded $CONFIG_FILE from config.example.yaml (mode 600) — set model_id and"
    info "base_url before trusting doctor. (kiwix_port is 8090: 8080 is the StudioForge panel's.)"
    SEEDED=1
fi
if [ "$MIGRATE_LEGACY" -eq 1 ]; then
    say "Optional: migrating the predecessor's settings (--migrate-localwiki)"
    if [ "${SEEDED:-0}" -eq 1 ]; then
        legacy_migrate_settings
    else
        info "config already existed — not overwriting it with legacy values."
    fi
fi

# The kiwix-serve image is pinned by tag (kiwix_image_tag). Pulling is a
# network action, so it is opt-in here; podman otherwise pulls on first use.
say "kiwix-serve image"
TAG="$(sed -n 's/^kiwix_image_tag:[[:space:]]*"\{0,1\}\([^"#[:space:]]*\).*/\1/p' "$CONFIG_FILE" | head -n1)"
TAG="${TAG:-$(sed -n 's/^kiwix_image_tag:[[:space:]]*"\{0,1\}\([^"#[:space:]]*\).*/\1/p' "$EXAMPLE_FILE" | head -n1)}"
if [ "$PULL" -eq 1 ]; then
    podman pull "$IMAGE_REPO:$TAG"
elif podman image exists "$IMAGE_REPO:$TAG" 2>/dev/null; then
    info "$IMAGE_REPO:$TAG is already present."
else
    info "$IMAGE_REPO:$TAG is not local yet. Pull it now with:"
    info "    podman pull $IMAGE_REPO:$TAG        (or re-run with --pull)"
    info "otherwise podman pulls it the first time the container is created."
fi

# ------------------------------------------------------------------- 3. unit
say "systemd --user unit"
mkdir -p "$UNIT_DIR"
install -m 644 "$UNIT_SRC" "$UNIT_DIR/$UNIT_NAME"
info "installed $UNIT_DIR/$UNIT_NAME (overwrote any previous copy)"
systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME"
info "enabled (WantedBy=default.target)"

# ------------------------------------------------------------------ 4. start
if [ "$MIGRATE_LEGACY" -eq 1 ]; then
    say "Optional: retiring the predecessor's container (--migrate-localwiki)"
    legacy_retire_container
fi

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
