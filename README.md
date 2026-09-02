# Kindred

Kindred is a runtime for an autonomous AI resident connected to an existing
Mouth host. It gives the resident a persistent inner state, a heartbeat,
activities, memory, and explicit boundaries for external capabilities.

The resident is the subject of the system. On each heartbeat, Kindred reads the
current world and approved conversation, updates a private working state,
optionally performs one activity step, validates the complete state, and then
persists the result. OpenClaw or Hermes provides the conversation surface;
portable capability packages add optional tools without changing the core graph.

> **Public Preview:** `v0.4.0` supports one resident on one trusted host. It is
> intended for operators who are comfortable reviewing local configuration and
> running pre-release software. This preview supports fresh installs only. Use a
> fresh user/HOME; the installer refuses to overwrite a different preview version.

[简体中文](README.zh-CN.md)

## Requirements

- One existing Mouth host:
  - OpenClaw `2026.6.10` (`aa69b12`) or `2026.7.1-2` (`0790d9f`), protocol 4;
  - Hermes `v2026.8.18`, package `0.20.4` (experimental).
- macOS 14 or later on Apple Silicon, or Ubuntu 24.04 on x86_64
- An existing OpenClaw agent/workspace or Hermes home with Persona files
- Credentials for a supported LLM and the map provider used during home setup

The release bundle includes CPython 3.11, its complete wheelhouse, the read-only
Web UI, and both Kindred Mouth Plugins. The target host does not need Python, Node,
pnpm, a package registry, or `sudo`. Draw is installed but disabled until the
operator explicitly configures its provider. Private platform integrations are
not part of the public release.

## Install

The short path downloads the versioned bootstrap and continues into the single
interactive installer:

```sh
curl -fsSL \
  https://github.com/skedup/kindred/releases/download/v0.4.0/install.sh \
  | sh
```

To inspect the bootstrap first:

```sh
curl -fLO https://github.com/skedup/kindred/releases/download/v0.4.0/install.sh
less install.sh
sh install.sh
```

The bootstrap verifies the selected offline bundle before installing it under
the current user's data directory. It does not install OpenClaw, modify the
system Python, detect a Mouth host, parse Persona, collect credentials, or start services. On a TTY it
continues into:

```sh
kindred install
```

After setup, useful operator commands are:

```sh
kindred doctor
kindred doctor --online
kindred start
kindred status
kindred logs
kindred stop
```

The Web UI is a trusted single-user observation surface and listens on loopback
by default. Remote exposure and authentication are outside Kindred's first
public preview.

`kindred uninstall` disconnects Kindred-owned services, binding, and the active
Mouth Plugin. It deliberately preserves the host workspace/home, Persona,
resident marker, configuration, database, memory, catalog, artifacts, and tick
history. There is no purge command.

Hermes support is experimental: it uses an approved direct transcript exported by
Hermes, injects the Mouth bundle once per approved turn, and publishes companion
Persona only on first use or content change. Hermes may retain that context in its
own `api_content` replay. Kindred does not read, replace, or synchronize Hermes
`MEMORY.md` or `memories/USER.md`. An uncertain `hermes send` result is not retried.

## Data Boundaries

Kindred does not send analytics, crash reports, or diagnostic telemetry to an
external service. By default it writes content-free token counts, durations,
statuses, and safe Provider/stage/tool identifiers to the local
`life/data/kindred-telemetry.db`. It never stores prompts, responses, tool
arguments/results, targets, or credentials there. Disable this local database
with `observability.enabled: false` or
`KINDRED_OBSERVABILITY_ENABLED=false`. Rows older than 90 days are logically
deleted; SQLite is not automatically vacuumed and this is not secure erase.
Uninstall preserves the telemetry database; after stopping Kindred, the
operator may manually delete that exact file. The loopback Web server exposes
content-free summaries and run/span detail under the read-only
`/api/observability/` API; it never creates or migrates this database while
serving a query. Functional providers still
receive the data required to do their work:

- the configured LLM receives the selected Persona and runtime context;
- the map provider receives the home address used for world resolution;
- the selected local Mouth host processes approved transcript history, Mouth
  context, and outbound dispatch;
- capabilities explicitly enabled by the operator may call their own providers.

Credentials and resident data stay outside the source distribution. Diagnostic
output is designed to report safe shapes rather than message, Persona,
credential, account, or target values.

## Development

```sh
uv run pytest
uv run ruff check src tests scripts
uv run mypy
uv build
```

The Web UI lives in `web/` and is read-only. Public life assets are packaged
under `kindred.life_assets`; external capabilities use the
`kindred.capability.v1` entry point. Release Web assets are built with Node
`22.18.0`, pnpm `11.7.0`, and the committed lockfile before building the wheel.
The macOS desktop spirit is developed and released independently in
[skedup/kindred-desktop](https://github.com/skedup/kindred-desktop).

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Report
security issues privately as described in [SECURITY.md](SECURITY.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
