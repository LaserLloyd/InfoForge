# Running InfoForge in containers

Image: `ghcr.io/laserlloyd/infoforge:<version>` (linux/amd64, linux/arm64). Docker or Podman.

InfoForge is a **CLI, not a daemon**: it runs, answers, exits. The long-running piece is
[`kiwix-serve`](https://github.com/kiwix/kiwix-tools) (Kiwix's own image,
`ghcr.io/kiwix/kiwix-serve`), which serves your `.zim` files. `deploy/compose/compose.yml` pairs the
two on a private network.

## README section (for the lead to insert)

> ### Run in Docker
>
> 1. **Compose files:** `cd deploy/compose && cp infoforge.config.example.yaml infoforge.config.yaml`
>    (set `model_id` and `base_url`; list your ZIM under `zims:`), and put the `.zim` in `./zims/`.
> 2. **Start kiwix:** `docker compose up -d kiwix` (Podman rootless: `KIWIX_USER=0 podman-compose up -d kiwix`).
> 3. **Ask:** `docker compose run --rm infoforge doctor`, then
>    `docker compose run --rm infoforge ask "Why is the sky blue?"`.
>
> InfoForge publishes no port of its own. kiwix-serve is on `127.0.0.1:8081` for browsing.
> Inside the container `infoforge serve ...` (the podman lifecycle) is **not used**: `kiwix_url`
> points at the `kiwix` service instead. Config, ZIMs and the event log are on volumes/mounts and
> survive image updates; `docker compose down -v` deletes the log volume (config and ZIMs are yours,
> on the host). Back up: `docker run --rm -v infoforge-state:/state:ro -v "$PWD":/backup alpine tar czf /backup/infoforge-state.tgz -C /state .`
> Details: [docs/deploy/docker.md](docs/deploy/docker.md).

## Quick start

```bash
cd deploy/compose
cp infoforge.config.example.yaml infoforge.config.yaml      # edit model_id, base_url, zims:
mkdir -p zims && cp ~/Downloads/wikipedia_en_simple_all_nopic.zim zims/
docker compose up -d kiwix
docker compose run --rm infoforge doctor
docker compose run --rm infoforge ask "What is the capital of France?"
```

`infoforge` is behind the `cli` profile, so `docker compose up` alone starts only kiwix; `compose run`
starts a one-off InfoForge container on the same network.

| Piece | Where | Notes |
| --- | --- | --- |
| kiwix-serve | service `kiwix`, `ghcr.io/kiwix/kiwix-serve:3.8.2`, container port 8080, published on `127.0.0.1:${KIWIX_PORT:-8081}` | `--skipInvalid` so one half-downloaded ZIM does not take it down |
| Config | `./infoforge.config.yaml` mounted read-only at `/config/config.yaml` (`INFOFORGE_CONFIG`) | git-ignored by `deploy/compose/.gitignore` |
| ZIMs | `${ZIM_DIR:-./zims}` mounted read-only at `/zims` (InfoForge) and `/data` (kiwix) | `zim_dir: /zims` in the container config |
| Event log | named volume `infoforge-state` at `/state` (`INFOFORGE_LOG=/state/log.jsonl`) | JSON lines, appended |
| LLM | your `base_url` (the example ships `http://set-me.invalid/v1` on purpose, so nothing talks to a host port until you choose) | `http://host.docker.internal:1234/v1` for a server on the host; `http://studioforge:1234/v1` inside the DisPatch stack |

## Rootless Podman and SELinux

kiwix-serve's image runs as uid 1001, which rootless Podman remaps to a subordinate uid that cannot
read a mode-700 home directory. The usual fix is the same one the host-mode `serve start` uses:
run it as container root, which maps to *you* (`KIWIX_USER=0`). The compose file's `:z` on the bind
mounts relabels them on SELinux hosts and is a no-op elsewhere.

## Known limit: `doctor` inside the stack

`infoforge doctor` also checks the **podman container** (`podman is not installed or not on PATH`),
which can never pass inside this image, so `doctor` exits 1 in the compose stack even when kiwix,
the search and the LLM rows pass. Read the rows; ignore `container`. (Fix belongs in `src/`: skip that
check when `kiwix_url` is explicitly configured. Reported to the owner of the code.) Likewise
`infoforge search`/`ask` will try to start the podman container if kiwix does not answer, and fail
with `podman is not installed`: when you see that, kiwix is down, not InfoForge.

## Your data survives updates

- Nothing stateful is in the image, which runs with a **read-only root filesystem** (`/tmp` is tmpfs).
- Config and ZIMs are host files you own; the event log is on the `infoforge-state` volume.
- There is **no database and no schema**, so there is nothing to migrate and no pre-migration
  snapshot: the only version-sensitive file is `config.yaml`, which you keep. `docker compose pull &&
  docker compose up -d` is safe; `docker compose down -v` deletes only the log.
- The container runs as uid 1000; the ZIM directory only needs to be readable by it.

## Backup and restore

Config and ZIMs are on the host: copy them. The log volume:

```bash
docker run --rm -v infoforge-state:/state:ro -v "$PWD":/backup alpine tar czf /backup/infoforge-state.tgz -C /state .
docker volume create infoforge-state
docker run --rm -v infoforge-state:/state -v "$PWD":/backup:ro alpine \
  sh -c 'tar xzf /backup/infoforge-state.tgz -C /state && chown -R 1000:1000 /state'
```

## Build

```bash
docker build --build-arg VERSION=2.0.0 -t ghcr.io/laserlloyd/infoforge:2.0.0 .
podman build --format docker --build-arg VERSION=2.0.0 -t ghcr.io/laserlloyd/infoforge:2.0.0 .
```

`uv.lock` is tracked here, so the image installs the locked set (`uv sync --locked`).
