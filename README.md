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
  [1] Paris — http://127.0.0.1:8080/content/wikipedia_en_all_nopic/A/Paris
  [2] France — http://127.0.0.1:8080/content/wikipedia_en_all_nopic/A/France
```

The pipeline is: **search the ZIMs** (a local `kiwix-serve` container) →
**rerank the hits** → **fetch the article text** → **build a grounded prompt**
("answer ONLY from these sources and cite the titles") → **ask an
OpenAI-compatible LLM** → **print the answer plus citations**.

Nothing leaves the machine. There is no embedding model, no vector store and no
network dependency beyond your own LLM endpoint — retrieval is Kiwix full-text
search plus a lexical reranker, which is fast, explainable, and good enough
because Wikipedia titles are already excellent keys.

**Why this exists.** An agent that cannot look something up will confabulate
instead, confidently. InfoForge gives it a lookup that either grounds the
answer or **fails loudly with a non-zero exit code** — never a plausible
sentence with nothing behind it.

---

## Requirements

- **Python 3.12+** and [uv](https://docs.astral.sh/uv/).
- **podman** (rootless is fine, and is what the flags below assume) to run
  `kiwix-serve`. Docker works if you adapt the container flags.
- At least one **`.zim` file** in `~/zims`. Get one from
  <https://library.kiwix.org> — `wikipedia_en_all_nopic.zim` (~100 GB with
  every article, no images) is the reference; `wikipedia_en_simple_all_nopic`
  is a few gigabytes and fine for trying this out.
- A running **OpenAI-compatible chat server** — LM Studio, llama.cpp's
  `llama-server`, vLLM, or a remote one on your own network.

---

## Quickstart

```bash
git clone https://github.com/LaserLloyd/InfoForge.git infoforge && cd infoforge  # scrub-ok: this project's own public repository URL

# 1. get a database (any ZIM from https://library.kiwix.org)
mkdir -p ~/zims
curl -L -o ~/zims/wikipedia_en_simple_all_nopic.zim \
  https://download.kiwix.org/zim/wikipedia/wikipedia_en_simple_all_nopic.zim

# 2. install the CLI, seed the config, install + start the service
deploy/install.sh

# 3. tell it about your LLM and your ZIM
$EDITOR ~/.config/infoforge/config.yaml

# 4. check everything is wired up, then ask
infoforge doctor
infoforge ask "Why is the sky blue?"
```

`deploy/install.sh` is idempotent — re-run it after every `git pull`. It never
overwrites an existing `~/.config/infoforge/config.yaml`.

---

## The CLI

| Command | What it does |
|---|---|
| `infoforge ask "…"` | The whole pipeline: retrieve, ground, answer, cite. `--json` for machine output, `--strict` to fail when the answer cites nothing, `--max-articles N` to override the config. |
| `infoforge search "…"` | Retrieval only — the ranked articles, no LLM. The fastest way to see *why* an answer came out the way it did. `--json` supported. |
| `infoforge serve start` | Bring the `infoforge-kiwix` container up (recreating it if the ZIM set changed). |
| `infoforge serve stop` | Stop the container. |
| `infoforge serve status` | running / exited / not created. |
| `infoforge serve recreate` | Force a rebuild of the container — the way to pick up a ZIM you just added. |
| `infoforge status` | One-line summary: container, ZIMs, endpoint. |
| `infoforge doctor` | The health table. Start here when anything is wrong. |
| `infoforge zim list` | The configured ZIMs, which exist on disk, which are enabled. |
| `infoforge zim check-update` | Ask the Kiwix library catalogue whether a newer dump exists. **Reports only — it never downloads** a hundred-gigabyte file behind your back. |
| `infoforge zimify <dir>` | Turn a folder of Markdown/HTML into a small `.zim` you can serve alongside Wikipedia. |

### Exit codes

They are the contract an agent scripts against:

| Code | Meaning |
|---|---|
| `0` | Answered, grounded in retrieved sources. |
| `2` | Config or usage error. |
| `3` | Nothing relevant found — **no answer was invented**. |
| `4` | `--strict`: the model answered without citing any source it was given. |
| `5` | No LLM was reachable. The retrieved sources are still printed, so search alone remains useful. |

`infoforge doctor` sits outside that table: it exits **1** when any health check
failed, and `0` when they all passed.

---

## Configuration

Real config lives **outside the repo** at `~/.config/infoforge/config.yaml`
(override with `INFOFORGE_CONFIG`). The repo ships `config.example.yaml` with
loopback placeholders and no real hosts. That separation is deliberate: your
endpoint, your model and your network are not this project's business, and a
config that lives in the working tree is a config that eventually gets
committed.

| Key | Default | Meaning |
|---|---|---|
| `model_id` | *(required)* | Model name the LLM server should use. |
| `base_url` | `http://127.0.0.1:1234/v1` | OpenAI-compatible API root; InfoForge POSTs to `{base_url}/chat/completions`. |
| `api_key` | `""` | Bearer token. The `Authorization` header is omitted entirely when empty. |
| `studioforge` | `false` | Set `true` when the endpoint is a StudioForge server; chat bodies then carry its scheduling `priority` field. |
| `fallbacks` | `[]` | List of `{model_id, base_url, api_key}` tried in order when the primary is unreachable. |
| `kiwix_bind` | `127.0.0.1` | Host address the container publishes on. Leave it on loopback unless you mean to serve the LAN. |
| `kiwix_port` | `8080` | Host port. |
| `kiwix_url` | *(derived)* | Base URL of kiwix-serve; computed from `kiwix_bind`/`kiwix_port` when unset. |
| `zim_dir` | `~/zims` | Where the `.zim` files live. Mounted into the container; never inside the repo. |
| `zims` | `[]` | The registry: `{file, name, enabled}` entries. Only `enabled` files that exist are served. |
| `max_articles` | `5` | How many reranked articles are fetched and fed to the model. |
| `max_chars` | `8000` | Characters of article text per source. |
| `search_pool` | `15` | Hits pulled from Kiwix before reranking. Larger pool, better top-5, slightly slower. |
| `timeout_s` | `180` | Timeout in seconds for **LLM requests only**. The kiwix HTTP timeouts are fixed constants and are not configurable. |
| `hold_wait_s` | `300` | How long to wait out an endpoint that answers "busy" (HTTP 503/429, e.g. StudioForge's `priority_hold`) before moving to the next fallback. `0` fails straight through. |

### The fallback chain

`fallbacks` exists for one specific failure: the machine hosting your big model
is asleep. InfoForge falls through to the next endpoint on a **connection or
5xx failure** — never on a well-formed refusal, because a model that answered
"the sources do not say" gave a correct answer and retrying it on a weaker
model would be shopping for a worse one.

---

## The kiwix container, and why the flags look like that

`infoforge serve start` runs, in effect:

```bash
podman run -d --name infoforge-kiwix --user root \
  -p 127.0.0.1:8080:8080 \
  -v ~/zims:/data:z \
  ghcr.io/kiwix/kiwix-serve -k /data/<file1>.zim /data/<file2>.zim
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
recreate`.

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
image, macOS), stop after `uv tool install .` and `infoforge serve start`.
Everything except autostart works exactly the same; you just bring the
container up yourself after a reboot.

---

## Using it from an agent

InfoForge has no agent framework of its own and does not want one — it is a
CLI with stable exit codes, which is the most portable tool interface there
is. Whatever runs your agents, give it a shell tool that calls:

```bash
infoforge ask "<the question>" --json
```

`--json` returns the answer, the source titles and their URLs, and the exit
code's meaning, so a wrapper does not have to parse prose.

Two rules are worth writing into the agent's own instructions, because they are
what make the tool trustworthy:

1. **A non-zero exit is a real answer.** Exit 3 means offline Wikipedia had
   nothing relevant. The agent must report that, not fall back to what it
   thinks it remembers.
2. **Citations are load-bearing.** The titles InfoForge prints are the evidence
   the answer stands on; pass them through to the user rather than
   summarising them away.

For read-only or exploratory use, `infoforge search "…" --json` gives an agent
the candidate articles without spending an LLM turn.

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

Drop the result into `zim_dir`, add it to `zims:` in your config, and
`infoforge serve recreate`.

---

## Troubleshooting

**Start with `infoforge doctor`.** It checks, in order: the config parses; the
ZIM directory and every enabled ZIM exist; the container is running; kiwix
answers HTTP; a full-text search actually returns hits; the primary LLM is
reachable; each fallback is reachable. It prints a table and exits non-zero if
any row failed — so the first red row is the thing to fix.

| Symptom | Usually |
|---|---|
| `no grounding found` (exit 3) | The ZIM is not being served. `infoforge zim list`, then `infoforge serve recreate`. A *partially downloaded* ZIM is skipped by `-k`, silently and correctly. |
| Container exits immediately | `podman logs infoforge-kiwix`. "Permission denied" on `/data` means the `--user root` or `:z` flag got lost; "Arguments did not match expected patterns" means it started with no ZIM paths. |
| `all LLM endpoints unreachable` (exit 5) | Is the server up at `base_url`, and is `model_id` the name it actually serves? The sources are still printed above the error. |
| Answers ignore the sources | Small models drift. Try `--strict` to make it a hard failure, raise `max_chars`, or lower `search_pool` so fewer marginal articles crowd the prompt. |
| Retrieval returns junk | `infoforge search "…"` shows the ranking. Disambiguation pages, "List of …" and `Category:` titles are already demoted; if a specific query is bad, the reranker's weights are in `retrieve.py`. |

---

## Repository layout

```
src/infoforge/       the package (config, kiwix, retrieve, extract, answer, cli, …)
tests/               pytest, no network
deploy/              systemd unit + install.sh
scripts/             scrub_check.py privacy gate + install-hooks.sh
config.example.yaml  the template for ~/.config/infoforge/config.yaml
```

### Contributing

```bash
scripts/install-hooks.sh          # once per clone
uv run pytest
```

`scripts/scrub_check.py` is a fail-closed scanner for private data — API keys,
private and Tailscale IP addresses, MagicDNS hostnames, absolute paths inside
somebody's home directory, and (in commit messages) AI-assistant session URLs
and co-author trailers. The hooks run it over the staged index and over every
commit message. `python3 scripts/scrub_check.py --selftest` verifies the
scanner itself; genuine false positives are silenced with an inline
`# scrub-ok: <reason>` comment on that line.

## Licence

MIT — see [LICENSE](LICENSE).
