#!/usr/bin/env bash
#
# Wire the privacy gate into git. Run once per clone:
#
#   scripts/install-hooks.sh
#
# Installs two hooks, both of which call scripts/scrub_check.py:
#
#   pre-commit  → --staged   scans the INDEX (not the working tree: staging a
#                            secret and then tidying the working copy is a real
#                            way to commit one by accident)
#   commit-msg  → --message  scans the message itself, which is published as
#                            loudly as the code and is where a host, a path or
#                            an assistant session URL gets pasted without
#                            thinking. House rule on this project: no AI
#                            co-author trailers and no session URLs in the log.
#
# Hooks are local to a clone and are never themselves committed, which is why
# this has to be a script you run rather than a file in .git/hooks/ you get for
# free. Re-running is safe; existing hooks are backed up alongside, not
# clobbered silently.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

command -v git >/dev/null 2>&1 || { echo "install-hooks.sh: git not on PATH" >&2; exit 1; }

HOOK_DIR="$(git rev-parse --git-path hooks)"
[ -n "$HOOK_DIR" ] || { echo "install-hooks.sh: not a git repository" >&2; exit 1; }
mkdir -p "$HOOK_DIR"

install_hook() {
    # One assignment per line: bash expands every word of a `local` statement
    # BEFORE assigning any of them, so `local name="$1" target="…$name"` reads
    # an unbound $name and dies under `set -u`.
    local name="$1"
    local body="$2"
    local target="$HOOK_DIR/$name"

    if [ -e "$target" ] && ! grep -q 'scrub_check.py' "$target"; then
        # Somebody else's hook is here. Keep it — silently overwriting another
        # tool's hook is how you get a mystery-broken commit flow next week.
        local backup="$target.pre-infoforge"
        if [ -e "$backup" ]; then
            echo "install-hooks.sh: $backup already exists; move it aside and re-run" >&2
            exit 1
        fi
        mv "$target" "$backup"
        echo "    existing $name hook preserved as $(basename "$backup")"
    fi

    printf '%s\n' "$body" > "$target"
    chmod +x "$target"
    echo "    installed $name"
}

install_hook pre-commit '#!/bin/sh
# InfoForge privacy gate — scans the STAGED content of this commit.
# Bypass (only when you are certain) with: git commit --no-verify
exec python3 "$(git rev-parse --show-toplevel)/scripts/scrub_check.py" --staged'

install_hook commit-msg '#!/bin/sh
# InfoForge privacy gate — scans the commit MESSAGE.
# Blocks credentials, private hosts/paths, and assistant session URLs or
# AI co-author trailers (house rule: the git log is a human record).
exec python3 "$(git rev-parse --show-toplevel)/scripts/scrub_check.py" --message "$1"'

echo
echo "Hooks installed in $HOOK_DIR"
echo "Verify the scanner itself with: python3 scripts/scrub_check.py --selftest"
