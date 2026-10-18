# Running InfoForge in containers

Image: `ghcr.io/laserlloyd/infoforge:<version>`, **built locally from the clone** by
`docker compose run`/`docker compose build` (the `Containers` workflow that would publish it to
GHCR for linux/amd64 and linux/arm64 has not been run yet). Docker or Podman.

InfoForge is a **CLI, not a daemon**: it runs, answers, exits. The long-running piece is
[`kiwix-serve`](https://github.com/kiwix/kiwix-tools) (Kiwix's own image,
`ghcr.io/kiwix/kiwix-serve`), which serves your `.zim` files. `deploy/compose/compose.yml` pairs the
two on a private network. The README's "Run in Docker" section is the three-step version of this page.

## Quick start

```bash
cd deploy/compose
cp infoforge.config.example.yaml infoforge.config.yaml      # edit model_id, base_url, zims:
mkdir -p zims
ZIM=$(curl -sL https://download.kiwix.org/zim/wikipedia/ | grep -o 'wikipedia_en-simple_all_nopic_[0-9-]*\.zim' | sort -u | tail -1)
curl -L -o zims/$ZIM https://download.kiwix.org/zim/wikipedia/$ZIM
sed -i.bak "s/wikipedia_en-simple_all_nopic_[0-9-]*\.zim/$ZIM/" infoforge.config.yaml && rm infoforge.config.yaml.bak
docker compose up -d kiwix
docker compose run --rm infoforge doctor
docker compose run --rm infoforge ask "What is the capital of France?"
```

`infoforge` is behind the `cli` profile, so `docker compose up` alone starts only kiwix; `compose run`
starts a one-off InfoForge container on the same network.

| Piece | Where | Notes |
| --- | --- | --- |
| kiwix-serve | service `kiwix`, `ghcr.io/kiwix/kiwix-serve:3.8.2`, container port 8080, published on `127.0.0.1:${KIWIX_PORT:-8090}` (the same default as a native install's `kiwix_port`) | `--skipInvalid` so one half-downloaded ZIM does not take it down |
| Config | `./infoforge.config.yaml` mounted read-only at `/config/config.yaml` (`INFOFORGE_CONFIG`) | git-ignored by `deploy/compose/.gitignore` |
| ZIMs | `${ZIM_DIR:-./zims}` mounted read-only at `/zims` (InfoForge) and `/data` (kiwix) | `zim_dir: /zims` in the container config |
| Event log | named volume `infoforge-state` at `/state` (`INFOFORGE_LOG=/state/log.jsonl`) | JSON lines, appended |
| LLM | your `base_url` (the example ships `http://set-me.invalid/v1` on purpose, so nothing talks to a host port until you choose) | `http://host.docker.internal:1234/v1` for a server on the host; `http://studioforge:1234/v1` inside the DisPatch stack |

## Rootless Podman and SELinux

kiwix-serve's image runs as uid 1001, which rootless Podman remaps to a subordinate uid that cannot
read a mode-700 home directory. The usual fix is the same one the host-mode `serve start` uses:
run it as container root, which maps to *you* (`KIWIX_USER=0`). The compose file's `:z` on the bind
mounts relabels them on SELinux hosts and is a no-op elsewhere.

## `doctor`, `search` and `ask` inside the stack

There is no podman in the image and none is needed: `infoforge doctor` reports the `container`
row as `podman not installed: skipped (using the kiwix-serve at kiwix_url)` and exits 0 when the
other rows pass (verified in the 2026-10 review). If kiwix is down, `search`/`ask` fail fast with
`error.code: kiwix_unreachable` ("nothing answers at http://kiwix:8080 and podman is not installed to
start kiwix-serve"): that means the `kiwix` service is down, not InfoForge; `docker compose ps kiwix`.

## Your data survives updates

- Nothing stateful is in the image, which runs with a **read-only root filesystem** (`/tmp` is tmpfs).
- Config and ZIMs are host files you own; the event log is on the `infoforge-state` volume.
- There is **no database and no schema**, so there is nothing to migrate and no pre-migration
  snapshot: the only version-sensitive file is `config.yaml`, which you keep. `docker compose pull &&
  docker compose up -d` is safe. `docker compose down` keeps the log volume; `docker compose --profile cli down -v`
  deletes it. A plain `down -v` does not, because the `infoforge` service that declares the volume sits
  behind the `cli` profile (checked with Compose v5.6.0: plain `down -v` left the volume, the profiled form removed it).
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
docker build --build-arg VERSION=2.1.0 -t ghcr.io/laserlloyd/infoforge:2.1.0 .
podman build --format docker --build-arg VERSION=2.1.0 -t ghcr.io/laserlloyd/infoforge:2.1.0 .
```

`uv.lock` is tracked here, so the image installs the locked set (`uv sync --locked`). `compose.yml`
builds the same image with `INFOFORGE_TAG` (default `2.1.0`, keep it equal to `pyproject.toml`'s
version) as both the tag and the `VERSION` label.
