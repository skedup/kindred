# Contributing to Kindred

Kindred accepts contributions through GitHub pull requests under Apache-2.0.
The first public preview does not require a CLA or DCO sign-off.

## Before You Start

- Use an issue for a bug, installation problem, or capability proposal.
- Keep resident data, Persona, messages, addresses, credentials, debug dumps,
  and private provider responses out of issues, commits, and fixtures.
- Keep core changes independent of a specific private platform.
- A capability package should use the `kindred.capability.v1` entry point and
  preserve Host authorization and safe trace boundaries.
- Capability availability must not silently hide an Activity.

## Development

Use synthetic fixtures and install the workspace dependencies with `uv`:

```sh
uv run ruff format --check src tests scripts
uv run ruff check src tests scripts
uv run mypy
uv run pytest
uv build
```

Web changes also require:

```sh
pnpm --dir web test
pnpm --dir web typecheck
python scripts/build_web.py build
```

Pull requests should explain the behavior change, tests run, data-flow impact,
and any new provider dependency. Avoid unrelated refactors and compatibility
layers for contracts that have not been released.
