# Changelog

All notable changes to InfoForge. Format: [Keep a Changelog](https://keepachangelog.com/);
versions follow [Semantic Versioning](https://semver.org/). Exit codes and the
`reason` strings of `ask --json` are a stable contract and have not changed
since 2.0.0.

## [2.1.0] - 2026-10-18

Hardening, a library API, and two new ways to consume InfoForge (MCP server and
DisPatch add-on). No new retrieval or generation features: no embeddings, no
new ranking.

### Added
- **Library API** `infoforge.api` (`search`, `ask`, `status`, `doctor`, `zim_list`,
  `zim_check_update`, `config_show`, `config_schema`) returning plain dicts; the
  CLI is now a thin printer over it. `search --json [--text]` output is
  byte-identical to 2.0 (golden-file test).
- **MCP server** `infoforge-mcp` (stdio; extra `infoforge[mcp]`, `mcp>=1.23,<3`) with
  five read-only tools: `search`, `ask`, `status`, `doctor`, `zim_list`.
- **DisPatch add-on** `infoforge_dispatch` (entry point `dispatch.extensions`,
  extra `infoforge[dispatch]`) registering the `wiki_search` live tool.
- **Error catalog** `infoforge.errors`: every exit code, `reason` and a new
  fine-grained `error.code` with message, fix and docs link. `--json` output
  carries `error: {code, message, fix, docs}`; `search --json` failures now print
  JSON too.
- **Settings schema** `infoforge.settings_schema` for all keys; `INFOFORGE_<KEY>`
  environment overrides for every key (precedence: CLI flag > env > file >
  default); a config *file* is optional when the environment supplies
  `model_id` and `base_url`; `infoforge config show [--json]` and
  `infoforge config schema [--json]`; unknown keys now warn.
- `search --no-ensure`: never creates, starts or recreates the container; fails
  fast (exit 2, `kiwix_unreachable`) if kiwix-serve is down.
- `kiwix_image_tag` setting (default `3.8.2`): the kiwix-serve image is pinned by
  tag instead of floating.
- `log_questions` setting (default `false`).
- `zims[].license` and CC BY-SA 4.0 attribution: `ask` prints an `Attribution:`
  line; `ask --json` adds `attribution` and a `license` per source.
- Generated reference docs `docs/reference/{cli,config,errors,mcp}.md`
  (`scripts/gen_docs.py`, drift-tested and checked in CI).
- `doctor`: config-file permission check when an `api_key` is set, an exposure
  warning for a non-loopback `kiwix_bind`, and the container check is skipped
  (not failed) when podman is not installed.
- `deploy/install.sh --uninstall` (keeps config, ZIMs and logs).
- `CHANGELOG.md`, `SECURITY.md`, `THIRD_PARTY_NOTICES.md`; README platform
  statement, network-use table and `search --text` documentation.
- Tests: socket/subprocess guard in `tests/conftest.py`; coverage for `doctor`,
  `cli`, the API, MCP, the add-on, and the podman-free bring-your-own-kiwix path.
  CI runs `pip-audit` and the docs-drift check.

### Changed
- **`ask --json` `error` is now an object** `{code, message, fix, docs}`; it was a
  string (`message` is the old string). Non-zero exits 4 and 6 now carry an
  `error` too. Everything else in the envelope is unchanged.
- **Default `kiwix_port` is 8090 for fresh configs** (`config.example.yaml`,
  `install.sh`). 8080 collides with the StudioForge panel. A config file that
  omits `kiwix_port` keeps 8080, so existing installs are not moved.
- `ask`/`search`: a question that starts with `-` is searched, not parsed as an
  option, and `--` is accepted; `--config=` inside a question cannot re-point the
  config. Abbreviated long options are no longer accepted.
- The config file is found via `$XDG_CONFIG_HOME` like `install.sh` does.
- The `book` segment of kiwix URLs is percent-encoded as a single path segment.
- The event log is mode 0600, rotated at 1 MB (3 files), and stores a keyed digest
  (`question_hmac`: HMAC-SHA-256 under a random per-install `question.key` kept 0600
  beside the log) and the length instead of the question text by default. An unkeyed
  hash would let anyone holding the log confirm a guessed question.
- `max_articles` passed to `ask`/`search` (CLI `--max-articles`, the library API, MCP)
  must be >= 1: a 0 or negative value is a `usage_error` (exit 2). It used to fall back
  to the config (0) or retrieve nothing and report `no_sources` (negative).
- `status` works without podman. `urllib3` 2.7.0 -> 2.8.0 (fixes three advisories).
- `install.sh`: the legacy `~/localwiki` migration is now an isolated, optional block.
- Test fixtures use ports 11234/11235/18080 instead of the live 1234/1235/8080.

### Fixed
- `enabled: "false"` (a quoted string) in `zims:` silently enabled the ZIM.
- `zims[].file` accepted backslashes (`a\b.zim`); it now must be a bare filename.
- The installer could seed a config the CLI never read when `XDG_CONFIG_HOME` was set.
- `zimify`: the `zimwriterfs` command it runs (or prints) lacked the `--illustration` and `--name`
  arguments that zim-tools 3.x makes mandatory, so the build failed with "too few arguments"; a
  48x48 `illustration.png` is now written into the prepared directory and both flags are passed.
- README install lines pointed at PyPI (`pip install 'infoforge[mcp]'`), where InfoForge is not
  published; they now install from the clone. `deploy/compose` defaulted `INFOFORGE_TAG` to `2.0.0`
  (now `2.1.0`) and published kiwix on 8081 (now 8090, the native default); `docs/deploy/docker.md`
  no longer claims `doctor` fails inside the stack.
- `infoforge-mcp` reports its version in the MCP initialize handshake (SDK 2.x).
- A `kiwix_url` with a path prefix (kiwix-serve `--urlRootLocation`, e.g. behind a
  reverse proxy) works: the root was read as the book name, so every article fetch
  404'd and retrieval found nothing.
- `ask` attribution found the configured `license` of a ZIM whose name needs
  percent-encoding (spaces, `+`, ...) again.
- `zimify .` no longer passes an empty `--title` (zimwriterfs refuses it).
- `install.sh --uninstall` no longer misses an installed CLI when `uv tool list | grep -q`
  trips over `pipefail`, and names the log path the CLI actually uses.
- `config show` labels a `kiwix_port` kept at 8080 for an existing file as
  `legacy_default`, not `default`.
- podman runs with stdin at /dev/null, so under `infoforge-mcp` (whose stdin is the
  protocol stream) the `status`/`doctor` tools cannot hand it to a child.
- Docs said `docker compose down -v` deletes the log volume; it does not (the `infoforge` service is
  behind the `cli` profile) — `docker compose --profile cli down -v` does. The containers workflow
  referenced `sigstore/cosign-installer@v4`, which does not exist; it now uses `@v3`.

## [2.0.0]

First packaged release: `ask`, `search` (with `--text`), `serve`, `status`,
`doctor`, `zim`, `zimify`; the 0/2/3/4/5/6 exit-code contract; the
`ANSWERED:` / `NOT_IN_SOURCES:` verdict.
