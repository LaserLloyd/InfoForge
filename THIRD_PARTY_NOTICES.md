# Third-party notices

InfoForge itself is MIT-licensed (see [LICENSE](LICENSE)). It uses, or invokes,
the components below. Versions are those pinned in `uv.lock` at the time of
release 2.1.0; `uv.lock` is authoritative, and each component's own licence text
ships with its package.

## Python dependencies (installed with InfoForge)

| Package | Version | Licence | Role |
|---|---|---|---|
| requests | 2.34.2 | Apache-2.0 | HTTP client for kiwix-serve and the LLM endpoint |
| urllib3 | 2.8.0 | MIT | HTTP transport under requests |
| certifi | 2026.7.22 | MPL-2.0 | CA bundle under requests (unmodified; source at <https://github.com/certifi/python-certifi>) |
| charset-normalizer | 3.5.1 | MIT | requests dependency |
| idna | 3.19 | BSD-3-Clause | requests dependency |
| PyYAML | 6.0.3 | MIT | config file parsing |

## Optional: the `[mcp]` extra (`infoforge-mcp`)

| Package | Version | Licence |
|---|---|---|
| mcp (and mcp-types) | 2.3.0 | MIT |
| pydantic, pydantic-core | 2.14.1 / 2.50.1 | MIT |
| starlette | 1.8.0 | BSD-3-Clause |
| uvicorn | 0.54.0 | BSD-3-Clause |
| sse-starlette | 3.5.0 | BSD-3-Clause |
| anyio, h11, jsonschema, referencing, attrs, rpds-py, python-multipart, httpx2, httpcore2, click, truststore, typing-extensions, typing-inspection, PyJWT, cryptography, cffi, pycparser, opentelemetry-api | see `uv.lock` | MIT, BSD-3-Clause, Apache-2.0 or PSF-2.0 (permissive) |

## Development only

pytest (MIT), pluggy (MIT), iniconfig (MIT), packaging (Apache-2.0 OR BSD-2-Clause),
Pygments (BSD-2-Clause), colorama (BSD-3-Clause). Built with hatchling (MIT).

## External programs (executed, never linked or bundled)

InfoForge runs these as separate processes with fixed argument lists. They are
not distributed with InfoForge, so their licences do not apply to it; they
apply to you when you install them.

| Program | Licence | Used for |
|---|---|---|
| kiwix-serve (container image `ghcr.io/kiwix/kiwix-serve`, pinned by `kiwix_image_tag`) | GPL-3.0 | serves ZIM archives and full-text search |
| podman | Apache-2.0 | runs the kiwix-serve container (managed mode only) |
| zimwriterfs (zim-tools) | GPL-3.0 | `infoforge zimify` |
| pandoc | GPL-2.0-or-later | `infoforge zimify` Markdown conversion (optional) |

## Content (not part of this repository)

ZIM archives are separate works with their own licences, which InfoForge does
not change.

- **Wikipedia, Wiktionary and the other Wikimedia projects:** CC BY-SA 4.0
  (<https://creativecommons.org/licenses/by-sa/4.0/>). Reuse requires
  attribution, a licence link and share-alike. `ask` prints an attribution line
  for these; see "Content licensing" in the README.
- **Stack Exchange archives:** CC BY-SA (the version depends on the dump date),
  with attribution requirements set by Stack Exchange.
- **Your own `zimify` archives:** whatever you chose for the source documents.
  Set `zims[].license` so attribution output is correct.

## Trademarks

Kiwix, Wikipedia, Wikimedia and other names belong to their owners; InfoForge
is independent of and unaffiliated with them.
