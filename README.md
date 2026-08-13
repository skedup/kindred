# Kindred

Kindred is an OpenClaw-native runtime for an autonomous AI resident. It gives the
resident a persistent inner state, a heartbeat, activities, memory, and explicit
boundaries for external capabilities.

The resident is the subject of the system. On each heartbeat, Kindred reads the
current world and approved conversation, updates a private working state,
optionally performs one activity step, validates the complete state, and then
persists the result. OpenClaw provides the conversation surface; portable
capability packages add optional tools without changing the core graph.

> **Public Preview:** `v0.2.1` supports one resident on one trusted host. It is
> intended for operators who are comfortable reviewing local configuration and
> running pre-release software. This preview supports fresh installs only. Use a
> fresh user/HOME; the installer refuses to overwrite a different preview version.

[简体中文](README.zh-CN.md)

## Requirements

- OpenClaw `2026.6.10` (`aa69b12`), Gateway protocol 4
- macOS 14 or later on Apple Silicon, or Ubuntu 24.04 on x86_64
- An existing OpenClaw agent and workspace with Persona files
- Credentials for a supported LLM and the map provider used during home setup

The release bundle includes CPython 3.11, its complete wheelhouse, the read-only
Web UI, and the Kindred Mouth Plugin. The target host does not need Python, Node,
pnpm, a package registry, or `sudo`. Draw is installed but disabled until the
operator explicitly configures its provider. Private platform integrations are
not part of the public release.

## Install

The short path downloads the versioned bootstrap and continues into the single
interactive installer:

```sh
curl -fsSL \
  https://github.com/skedup/kindred/releases/download/v0.2.1/install.sh \
  | sh
```

To inspect the bootstrap first:

```sh
curl -fLO https://github.com/skedup/kindred/releases/download/v0.2.1/install.sh
less install.sh
sh install.sh
```

The bootstrap verifies the selected offline bundle before installing it under
the current user's data directory. It does not install OpenClaw, modify the
system Python, parse Persona, collect credentials, or start services. On a TTY it
continues into:

```sh
kindred openclaw install
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

`kindred openclaw uninstall` disconnects Kindred-owned services, binding, and
the Mouth Plugin. It deliberately preserves the OpenClaw workspace, Persona,
resident marker, configuration, database, memory, catalog, artifacts, and tick
history. There is no purge command.

## Data Boundaries

Kindred does not add analytics, crash reporting, or diagnostic telemetry.
Functional providers still receive the data required to do their work:

- the configured LLM receives the selected Persona and runtime context;
- the map provider receives the home address used for world resolution;
- the local OpenClaw Gateway processes approved transcript history, Mouth
  context, and outbound dispatch;
- capabilities explicitly enabled by the operator may call their own providers.

Credentials and resident data stay outside the source distribution. Diagnostic
output is designed to report safe shapes rather than message, Persona, token,
account, or target values.

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

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Report
security issues privately as described in [SECURITY.md](SECURITY.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
