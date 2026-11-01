# Security policy

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

Use **Report a vulnerability** under the repository's Security tab (GitHub
private vulnerability reporting:
https://github.com/LaserLloyd/InfoForge/security/advisories/new). <!-- scrub-ok: public repo URL -->
Include the InfoForge version (`infoforge --version`), what you did, what you
expected and what happened. You will get an acknowledgement; there is no bug bounty.

## Supported versions

Only the latest 2.x release is supported.

## Threat model, in short

InfoForge is a local, single-user tool. It has no listener of its own, no
accounts and no telemetry. What it does expose:

- **The kiwix container.** `kiwix-serve` has no authentication. The container
  publishes on `kiwix_bind` (default `127.0.0.1`); setting it to `0.0.0.0` or a
  LAN address serves the whole library to that network, and `infoforge doctor`
  warns when you do. The container runs as root *inside* the container (needed
  so rootless podman can read your ZIMs), mapped to your own user on the host.
- **Your LLM endpoint.** Questions and retrieved article text are sent to
  `base_url`. `api_key` is held in the config file in plaintext; keep that file
  mode 600 (`doctor` checks) or use `INFOFORGE_API_KEY` (on a Windows bind mount, which
  cannot carry modes, use the environment variable). It is never written to
  the event log and `config show` redacts it. Credentials embedded in a
  `base_url` (`https://user:pass@host`) are redacted to `***@` in the event log,
  warnings, `doctor` output and `config show`; prefer `api_key` or
  `INFOFORGE_API_KEY`.
- **Untrusted text.** Article text goes into the prompt verbatim, so an article
  can attempt prompt injection. InfoForge's tools have no side effects beyond
  returning text, but the model's verdict marker can be spoofed by article
  content (the impact is a wrong exit code, not code execution).
- **Command line.** `infoforge ask/search` treat everything that is not one of
  their own flags as the question, so a caller passing user text cannot have it
  parsed as an option; callers should still put `--` before user text.
- **Config files.** `zims[].file` must be a bare filename (no `/` or `\`).
  `zimify` follows symlinks in the folder you give it and writes `<out>.zim.prep/`
  next to the output; do not point it at untrusted input.
- **Event log.** `log.jsonl` is mode 0600 and stores a keyed digest (HMAC under a
  random per-install `question.key`, mode 0600, beside the log) and the length of
  each question by default, not the text. Whoever can read `question.key` can
  still test guesses against the digests, so treat the log directory as private.
  While `log_questions` is off, InfoForge also rewrites `log.jsonl` and its rotated
  copies once per process (best effort) to remove question text, question hashes
  and article titles left by 2.0 or by an earlier `log_questions: true` period;
  old records keep only the question length, and a `log_scrub` event records that
  it ran. Rotation and this rewrite take a `log.jsonl.lock` file.
- **Supply chain.** The kiwix image is pinned by tag, not `latest`; digest
  pinning is not done. CI runs `pip-audit` against the locked dependency set.
- **Outbound connections** are listed in the README ("Network use").

## Out of scope

Anything that requires the attacker to already control your config file, your
LLM server or your ZIM files; denial of service by a very large ZIM; the
security of kiwix-serve, podman or the LLM server themselves.
