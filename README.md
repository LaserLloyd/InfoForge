# InfoForge

**Grounded answers from an offline Wikipedia, for local AI agents.**

InfoForge sits between a question and a local LLM and refuses to let the model
answer from memory. Every answer is built from articles it actually retrieved
out of a [Kiwix](https://kiwix.org) **ZIM** database — a full offline Wikipedia
dump, a Wiktionary, a Stack Exchange archive, or a ZIM you built yourself — and
every answer names the articles it used.

```console
$ infoforge ask "What is the capital of France?"
Paris is the capital and most populous city of France …

Citations:
[1] Paris — http://127.0.0.1:8090/content/wikipedia_en_all_nopic/A/Paris
[2] France — http://127.0.0.1:8090/content/wikipedia_en_all_nopic/A/France

Attribution: Paris, France: from Wikipedia and sister projects, licensed CC BY-SA 4.0 (…)
```

The pipeline is: **search the ZIMs** (a local `kiwix-serve`) → **rerank the
hits** → **fetch the article text** → **build a grounded prompt** ("answer ONLY
from these sources and cite the titles") → **ask an OpenAI-compatible LLM** →
**print the answer plus citations**.

There is no embedding model, no vector store and no index of InfoForge's own —
retrieval is Kiwix's built-in full-text search plus a lexical reranker, which is
fast, explainable, and good enough because Wikipedia titles are already
excellent keys. No telemetry, no accounts. What does touch the network is
listed under [Network use](#network-use) — it is not "nothing", and you should
know exactly what it is.

**Why this exists.** An agent that cannot look something up will confabulate
instead, confidently. InfoForge gives it a lookup that either grounds the
answer or **fails loudly with a non-zero exit code** — never a plausible
sentence with nothing behind it.

**Ways to use it:** the [CLI](#the-cli), an [MCP server](#mcp-server) for any
MCP client, a [DisPatch add-on](#dispatch-add-on), or the [Python
API](#python-api).

---

## Platform support (read this first)

| | Status |
|---|---|
| **Linux** with podman and systemd `--user` | The supported, tested setup. `deploy/install.sh` does everything. |
| **Any OS**, bring your own `kiwix-serve` | Works. Run `kiwix-serve` however you like (native binary, Docker, another machine), set `kiwix_url` to it, and **do not use `infoforge serve`**. InfoForge needs only Python 3.12+ and an LLM endpoint; podman is never touched. `infoforge doctor` reports the container check as skipped. Tested without podman. |
| macOS / Windows with the managed container | Not supported. The container half uses rootless podman, `--user root` and an SELinux `:z` volume flag, and a drive-letter `zim_dir` breaks podman's `-v` parsing. |

The bring-your-own mode needs no config *file* either: set `INFOFORGE_MODEL_ID`,
`INFOFORGE_BASE_URL`, `INFOFORGE_KIWIX_URL`, `INFOFORGE_ZIM_DIR` and
`INFOFORGE_ZIMS` (a JSON list) and run `infoforge search --no-ensure …`.

---

## Requirements

- **Python 3.12+** and [uv](https://docs.astral.sh/uv/).
- A running **kiwix-serve**: either the managed container (needs **podman**,
  Linux — see above) or one you run yourself.
- At least one **`.zim` file** in `~/zims`. Get one from
  <https://library.kiwix.org> — `wikipedia_en_all_nopic.zim` (~100 GB with
  every article, no images) is the reference; `wikipedia_en_simple_all_nopic`
  is a few gigabytes and fine for trying this out.
- A running **OpenAI-compatible chat server** — LM Studio, llama.cpp's
  `llama-server`, vLLM, StudioForge, or a remote one on your own network.

---

## Quickstart (Linux, podman)

```bash
git clone https://github.com/LaserLloyd/InfoForge.git infoforge && cd infoforge  # scrub-ok: this project's own public repository URL

# 1. get a database (any ZIM from https://library.kiwix.org)
mkdir -p ~/zims
curl -L -o ~/zims/wikipedia_en_simple_all_nopic.zim \
  https://download.kiwix.org/zim/wikipedia/wikipedia_en_simple_all_nopic.zim

# 2. pull the pinned kiwix-serve image (the installer does not pull for you)
podman pull ghcr.io/kiwix/kiwix-serve:3.8.2

# 3. install the CLI, seed the config, install + start the service
deploy/install.sh

# 4. tell it about your LLM and your ZIM
$EDITOR ~/.config/infoforge/config.yaml

# 5. check everything is wired up, then ask
infoforge doctor
infoforge ask "Why is the sky blue?"
```

`deploy/install.sh` is idempotent — re-run it after every `git pull`. It never
overwrites an existing config. `deploy/install.sh --uninstall` removes the
service, the container and the CLI and **keeps your config, ZIMs and logs**.

The kiwix image is **pinned by tag** (`kiwix_image_tag`, default `3.8.2`), not
`latest`, so a pull can never silently change the server. Pull the tag yourself
(step 2); if you skip it, podman pulls it on first container creation. To
upgrade kiwix-serve: `podman pull ghcr.io/kiwix/kiwix-serve:<tag>`, set
`kiwix_image_tag`, then `infoforge serve recreate`.

---

## The CLI

Full reference: [docs/reference/cli.md](docs/reference/cli.md) (generated from
the code).

| Command | What it does |
|---|---|
| `infoforge ask "…"` | The whole pipeline: retrieve, ground, answer, cite. `--json` for machine output, `--strict` to fail when the answer cites nothing, `--max-articles N` to override the config. Always ensures kiwix is up (starts the container if needed). |
| `infoforge search "…"` | Retrieval only — the ranked articles, no LLM. The fastest way to see *why* an answer came out the way it did. `--json` prints `{"query", "sources": [{title, url, score, chars}]}`; **`--json --text`** adds each source's extracted article text (already cut around the query's keywords) so a caller can answer with its own model. `--max-articles N` overrides the config. **`--no-ensure`** never creates, starts or recreates the container: if kiwix-serve is down it fails fast (exit 2, `error.code: kiwix_unreachable`) — what a caller on a deadline wants. |
| `infoforge serve start` | Bring the `infoforge-kiwix` container up (recreating it if the ZIM set changed). |
| `infoforge serve stop` | Stop the container. |
| `infoforge serve status` | running / exited / not created. `--json` supported. |
| `infoforge serve recreate` | Force a rebuild of the container — the way to pick up a ZIM you just added. |
| `infoforge status` | Container, ZIMs, endpoint. `--json` supported. Works without podman (`state: unavailable`). |
| `infoforge doctor` | The health table. Start here when anything is wrong. `--json` supported. |
| `infoforge config show` | The effective settings, where each value came from (`cli` / `env` / `file` / `default`), secrets redacted. `--json` supported. |
| `infoforge config schema` | Every setting with type, default, range and environment variable. `--json` prints a JSON Schema. |
| `infoforge zim list` | The configured ZIMs, which exist on disk, which are enabled. `--json` supported. |
| `infoforge zim check-update` | Ask the Kiwix library catalogue whether a newer dump exists. **Reports only — it never downloads** a hundred-gigabyte file behind your back. |
| `infoforge zimify <dir>` | Turn a folder of Markdown/HTML into a small `.zim` you can serve alongside Wikipedia. |

Global: `--config PATH` (which config file), `--version`.

### Questions that start with a dash

For `ask` and `search`, only the command's own flags are flags; **everything
else is part of the question**. `infoforge search -5 degrees in kelvin` and
`infoforge search --version` search for exactly that text, and `--config=…` in
a question cannot re-point the config. Flags may come before or after the
question, and a literal `--` ends flag parsing explicitly. Anything that passes
user text on a command line should use it:

```bash
infoforge search --json --text --no-ensure --max-articles 3 -- "$USER_TEXT"
```

### Exit codes

They are the contract an agent scripts against (unchanged since 2.0):

| Code | `reason` (`--json`) | Meaning |
|---|---|---|
| `0` | `answered` | Answered, grounded in retrieved sources. |
| `1` | — | `doctor` found a failing check (or an unexpected crash). |
| `2` | `config` | Config, usage or infrastructure error: a bad or missing config, a question that is empty, kiwix-serve unreachable, podman problems, a `zimify` failure, or an LLM 4xx. The finer `error.code` says which. |
| `3` | `no_sources` | Nothing relevant found — **no answer was invented**. |
| `4` | `uncited` | `--strict`: the model answered without citing any source it was given. |
| `5` | `llm_unreachable` | No LLM was reachable. The retrieved sources are still printed, so search alone remains useful. |
| `6` | `not_in_sources` | Articles were found, but the model says **they do not contain the answer**. Its one-line explanation and the sources are still printed. Applies with or without `--strict`. |

How exit 6 is decided: the prompt tells the model to open its reply with
`ANSWERED:` or `NOT_IN_SOURCES:`; InfoForge reads that marker (tolerating
markdown, case and a leaked reasoning preamble — the last marker wins) and
strips it from the printed answer. If a model omits the marker, the first
sentence of the reply is checked against common refusal phrasing instead, and
`--json` reports `verdict_source: "fallback"`. A refusal usually names an
article ("Eiffel Tower does not mention…"), which is why the citation check on
its own could never catch one.

With `--json`, `ask` prints **one JSON object on every exit** except a crash —
always with `ok`, `exit_code`, `reason`, `question` and `sources` (empty when
retrieval found nothing); a successful or exit-6 run adds `answer`, `verdict`,
`verdict_source`, `model_id`, `cited`, `uncited`; every non-zero exit adds
`error: {code, message, fix, docs}`. Each source row carries its `license`, and
`attribution` is a ready-made credit line. `ok` is `true` only for exit 0.
`search --json` failures print the same `ok`/`exit_code`/`reason`/`error` shape
(with `query` and empty `sources`) on stdout.

> 2.1 change: `error` in `ask --json` used to be a plain string; it is now the
> `{code, message, fix, docs}` object (`message` is the old string). Exit codes
> and `reason` strings did not change. The catalog is
> [docs/reference/errors.md](docs/reference/errors.md).

---

## Configuration

Real config lives **outside the repo** at
`$XDG_CONFIG_HOME/infoforge/config.yaml` (`~/.config/infoforge/config.yaml` when
unset); `--config PATH` or `INFOFORGE_CONFIG` pick another. The repo ships
`config.example.yaml` with loopback placeholders and no real hosts. That
separation is deliberate: your endpoint, your model and your network are not
this project's business, and a config that lives in the working tree is a config
that eventually gets committed.

Every key is declared once in `src/infoforge/settings_schema.py` and every one
can be set from the environment as **`INFOFORGE_<KEY>`** (list-valued keys take a
JSON list). Precedence: **CLI flag > environment > file > default.**
`infoforge config show` tells you which layer won. The full table — type,
default, range, environment variable — is
[docs/reference/config.md](docs/reference/config.md); the keys at a glance:

| Key | Default | Meaning |
|---|---|---|
| `model_id` | *(required)* | Model name the LLM server should use. |
| `base_url` | *(required)* | OpenAI-compatible API root; InfoForge POSTs to `{base_url}/chat/completions`. |
| `api_key` | `""` | Bearer token. Omitted from requests when empty. Prefer `INFOFORGE_API_KEY` over plaintext in the file; `doctor` flags a readable file that holds one. |
| `studioforge` | `false` | Set `true` when the endpoint is a StudioForge server (adds its scheduling `priority`). |
| `fallbacks` | `[]` | `{model_id, base_url, api_key}` tried in order when the primary is unreachable. |
| `kiwix_bind` | `127.0.0.1` | Host address the container publishes on. Leave it on loopback: kiwix-serve has no authentication. |
| `kiwix_port` | `8090` (see below) | Host port. |
| `kiwix_url` | *(derived)* | Base URL of kiwix-serve. Set it to use your own server. |
| `kiwix_image_tag` | `3.8.2` | Pinned kiwix-serve image tag. |
| `zim_dir` | `~/zims` | Where the `.zim` files live. |
| `zims` | `[]` | The registry: `{file, name, enabled, license}`. `file` is a bare filename (no `/`, no `\`). |
| `max_articles` / `max_chars` / `search_pool` | `5` / `8000` / `15` | Retrieval tuning. |
| `timeout_s` / `hold_wait_s` | `180` / `300` | LLM timeout; how long to wait out a busy endpoint. |
| `log_questions` | `false` | Store question text in the event log (default: hash and length only). |

### Ports

InfoForge opens no listening port of its own. The kiwix container publishes on
`kiwix_bind:kiwix_port`. **8080 is the StudioForge panel's port**, so a
co-located install collides with a default of 8080. Fresh configs
(`config.example.yaml`, `install.sh`) therefore use **8090**. A config file that
*omits* `kiwix_port` keeps the old value of 8080 — upgrading never moves a
running container; set the key explicitly if you hit the collision.

### The fallback chain

`fallbacks` exists for one specific failure: the machine hosting your big model
is asleep. InfoForge falls through to the next endpoint on a **connection or
5xx failure** — never on a well-formed refusal, because a model that answered
"the sources do not say" gave a correct answer and retrying it on a weaker
model would be shopping for a worse one.

---

## Network use

InfoForge sends no telemetry. These are *all* the connections it makes:

| To | When | Why |
|---|---|---|
| kiwix-serve (`kiwix_url`, loopback by default) | every `ask`/`search` | suggest, full-text search, article fetch |
| your LLM (`base_url`, plus any `fallbacks`) | `ask`, and `doctor` (a `GET /models`) | the answer; your question and the retrieved article text are sent there |
| `library.kiwix.org` | only `infoforge zim check-update` | compare your ZIM dates with the catalogue (report only) |
| `ghcr.io` (via podman) | only when the container is created and the pinned image is not local | pull `ghcr.io/kiwix/kiwix-serve:<kiwix_image_tag>` |

If your `base_url` is a remote host, questions leave the machine to reach it —
point it at a local server if that matters.

## Privacy

The event log (`~/.local/state/infoforge/log.jsonl`) is created mode `0600`,
rotated at 1 MB (3 files kept) and, by default, stores **no question text** —
only a SHA-256 prefix and the length, plus counts. Set `log_questions: true` to
keep the text and article titles. `api_key` is never logged, and
`infoforge config show` prints it as `***`.

## Content licensing

Wikipedia, Wiktionary and the other Wikimedia ZIMs are
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/); Stack Exchange
archives carry their own CC BY-SA terms. InfoForge's code is MIT, but **what it
prints from a ZIM is not**: if you republish it, you owe attribution and a link
to the licence. `ask` output therefore ends with an attribution line and every
`--json` source row has a `license` field. The per-ZIM licence string comes from
the registry (`zims[].license`); Wikimedia-family ZIMs default to `CC BY-SA 4.0`
and anything else is reported as `unknown` until you say what it is. `search`
output is unchanged (it carries no licence data) — attribute it yourself.

---

## The kiwix container, and why the flags look like that

`infoforge serve start` runs, in effect:

```bash
podman run -d --name infoforge-kiwix --user root \
  -p 127.0.0.1:8090:8080 \
  -v ~/zims:/data:z \
  ghcr.io/kiwix/kiwix-serve:3.8.2 -k /data/<file1>.zim /data/<file2>.zim
```

Every unusual flag there is load-bearing on a rootless-podman + SELinux host,
and each one was added because the container failed without it:

- **`--user root`** — under rootless podman, container root maps to *your* host
  user, which is the only identity that can traverse a mode-700 home directory
  and read your ZIMs. The image's default user (uid 1001) is remapped into the
  subuid range, is nobody as far as the host is concerned, and gets
  `Permission denied` on `/data`. This is *less* privilege than it looks like,
  not more.
- **`:z` on the bind mount** — with SELinux enforcing, an unlabelled bind mount
  is unreadable from inside the container even by root. `:z` relabels it as
  shared container content. Without it, kiwix-serve cannot open `/data` at all.
- **Explicit `/data/<file>.zim` arguments** — the image's entrypoint otherwise
  runs `kiwix-serve --port=8080` with no path and dies with "Arguments did not
  match expected patterns". The file list comes from the `zims:` registry
  (enabled entries that actually exist); if none match, InfoForge refuses to
  start the container and says so — an empty file list is a configuration
  error, not something to paper over by serving whatever happens to be in
  `zim_dir`.
- **`-k` (`--skipInvalid`)** — a partially downloaded ZIM is invalid, and
  without this flag one in-progress download takes the whole server down. With
  it, the finished databases keep working.

**Recreate, don't restart.** kiwix-serve does not hot-reload its file list, so
InfoForge recreates the container whenever the enabled-ZIM set changed *or* any
served `.zim` is newer than the container's start time. Finish a download, and
the next `infoforge ask` picks it up on its own. To force it: `infoforge serve
recreate`. (`search --no-ensure` never does this — it fails fast instead.)

**Search uses `format=xml`, not `format=json`.** Some kiwix-serve builds answer
`&format=json` with an HTML page; the XML/Atom endpoint is the one that behaves
consistently across builds, so that is what the client parses.

---

## Autostart

The container has no podman restart policy, so a systemd `--user` unit brings
it back after a reboot. `deploy/install.sh` installs and enables it:

```console
$ systemctl --user status infoforge.service     # Type=oneshot, RemainAfterExit=yes
$ systemctl --user restart infoforge.service    # re-asserts `serve start`
```

Restarting the unit is also the right way to revive a stopped or missing
container. Enable lingering (`loginctl enable-linger "$USER"`) if you want
kiwix up before you log in.

**No systemd user session?** On hosts without one (a container, a minimal
image), stop after `uv tool install .` and `infoforge serve start`.
Everything except autostart works exactly the same; you just bring the
container up yourself after a reboot.

---

## Using it from an agent

### CLI

Whatever runs your agents, give it a shell tool that calls:

```bash
infoforge ask --json -- "<the question>"
```

`--json` returns the answer, the source titles and their URLs, and the exit
code plus a stable `reason` string, on every exit — so a wrapper does not have
to parse prose. For read-only lookups that answer with your own model, use
`infoforge search --json --text --no-ensure -- "<query>"`.

Two rules are worth writing into the agent's own instructions, because they are
what make the tool trustworthy:

1. **A non-zero exit is a real answer.** Exit 3 means offline Wikipedia had
   nothing relevant; exit 6 means it had articles but they do not answer the
   question. The agent must report that, not fall back to what it
   thinks it remembers.
2. **Citations are load-bearing.** The titles InfoForge prints are the evidence
   the answer stands on; pass them through to the user rather than
   summarising them away.

### MCP server

```bash
pip install 'infoforge[mcp]'        # or: uv tool install 'infoforge[mcp]'
infoforge-mcp [--config PATH]       # stdio
```

```json
{"mcpServers": {"infoforge": {"command": "infoforge-mcp"}}}
```

Five **read-only** tools — `search`, `ask`, `status`, `doctor`, `zim_list` — over
the same library API as the CLI ([reference](docs/reference/mcp.md)). They never
create or recreate the container (a dead kiwix is a fast `kiwix_unreachable`
result), and there is deliberately no tool for `serve` or `zimify`. Requires
`mcp>=1.23,<3`.

### DisPatch add-on

```bash
uv pip install --python .venv/bin/python 'infoforge[dispatch]'   # in DisPatch's environment
```

The `dispatch.extensions` entry point `infoforge` registers one live tool,
`wiki_search(query, max_articles)`, backed by `api.search` with
`ensure=False` (read-only, never starts podman) and run in a worker thread.
Name it in a bot's `tools:` list (and in `tools_locked` to allow it on a
Safe-Mode device). InfoForge itself must be configured (`infoforge doctor`).

### Python API

```python
from infoforge import api

api.search("Emu War", text=True)      # {"query": ..., "sources": [{title, url, score, chars, text}]}
api.ask("Who won the Emu War?")       # result envelope: ok, exit_code, reason, answer, sources, error, attribution
api.status(); api.doctor(); api.zim_list()
```

Blocking calls; use `asyncio.to_thread` from async code. `search` raises
`infoforge.errors.InfoForgeError` (with `.code`, `.exit_code`); `ask` always
returns the envelope.

---

## Building your own database

```bash
infoforge zimify ./notes
```

`zimify` converts a folder of Markdown/HTML into a small `.zim`: it prepares an
HTML tree (converting Markdown with `pandoc` or `markdown` when one is
installed), builds an `index.html` welcome page linking every document, and
then calls `zimwriterfs` if it is available. If it is not, the prepared
directory is kept and the exact `zimwriterfs` command is printed for you to run
later (`zim-tools` from <https://github.com/openzim/zim-tools>).

Drop the result into `zim_dir`, add it to `zims:` in your config (with a
`license:` for your own content), and `infoforge serve recreate`.

---

## Troubleshooting

**Start with `infoforge doctor`.** It checks, in order: the config parses (and
is not world-readable if it holds a key); the ZIM directory and every enabled
ZIM exist; the container is running (skipped without podman); kiwix answers
HTTP; a full-text search actually returns hits; the primary LLM is reachable;
each fallback is reachable. It prints a table and exits non-zero if any row
failed — so the first red row is the thing to fix.

| Symptom | Usually |
|---|---|
| `no sources found` (exit 3) | The ZIM is not being served. `infoforge zim list`, then `infoforge serve recreate`. A *partially downloaded* ZIM is skipped by `-k`, silently and correctly. |
| `kiwix_unreachable` (exit 2) | kiwix-serve is down. `infoforge serve start`, or check `kiwix_url`. |
| Container exits immediately | `podman logs infoforge-kiwix`. "Permission denied" on `/data` means the `--user root` or `:z` flag got lost; "Arguments did not match expected patterns" means it started with no ZIM paths. |
| `all LLM endpoints unreachable` (exit 5) | Is the server up at `base_url`, and is `model_id` the name it actually serves? The sources are still printed above the error. |
| Answers ignore the sources | Small models drift. Try `--strict` to make it a hard failure, raise `max_chars`, or lower `search_pool` so fewer marginal articles crowd the prompt. |
| Retrieval returns junk | `infoforge search "…"` shows the ranking. Disambiguation pages, "List of …" and `Category:` titles are already demoted; if a specific query is bad, the reranker's weights are in `retrieve.py`. |

Every error code, with its fix, is in [docs/reference/errors.md](docs/reference/errors.md).

---

## Repository layout

```
src/infoforge/          the package: config + settings_schema + errors, kiwix, retrieve,
                        extract, answer, api, cli, doctor, logging_, zimify, mcp_server
src/infoforge_dispatch/ the DisPatch add-on (wiki_search)
tests/                  pytest; no network, no podman (enforced by tests/conftest.py)
docs/reference/         cli / config / errors / mcp pages, GENERATED by scripts/gen_docs.py
deploy/                 systemd unit + install.sh (--uninstall keeps your data)
scripts/                gen_docs.py, scrub_check.py privacy gate, install-hooks.sh
config.example.yaml     the template for ~/.config/infoforge/config.yaml
CHANGELOG.md  SECURITY.md  THIRD_PARTY_NOTICES.md
```

### Contributing

```bash
scripts/install-hooks.sh          # once per clone
uv sync --extra mcp
uv run pytest
python scripts/gen_docs.py        # after changing the CLI, config schema, errors or MCP tools
```

CI also runs `python scripts/gen_docs.py --check` and `pip-audit`.

`scripts/scrub_check.py` is a fail-closed scanner for private data — API keys,
private and Tailscale IP addresses, MagicDNS hostnames, absolute paths inside
somebody's home directory, and (in commit messages) AI-assistant session URLs
and co-author trailers. The hooks run it over the staged index and over every
commit message. `python3 scripts/scrub_check.py --selftest` verifies the
scanner itself; genuine false positives are silenced with an inline
`# scrub-ok: <reason>` comment on that line.

Security reports: see [SECURITY.md](SECURITY.md). Third-party components and
their licences: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). History:
[CHANGELOG.md](CHANGELOG.md).

## Licence

MIT — see [LICENSE](LICENSE). The content you retrieve has its own licence; see
[Content licensing](#content-licensing).
