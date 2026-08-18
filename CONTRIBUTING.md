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

Use synthetic fixtures. Development and CI use exactly `uv 0.8.13`, as pinned
by `tool.uv.required-version` and `distribution/release-inputs.json`. Confirm the
tool identity, verify the committed lockfile, and install without re-resolving:

```sh
uv --version
uv lock --check
uv sync --frozen
```

Run the Python checks against the frozen environment:

```sh
uv run --frozen ruff format --check src tests scripts packages
uv run --frozen ruff check src tests scripts packages
uv run --frozen mypy
python scripts/ci/check_prompt_asserts.py
python scripts/ci/run_coverage.py
python scripts/build_web.py build
uv build --all-packages
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

CI selects gates from the complete pull-request diff:

- changes only under `docs/`, or to the root `README*`, `CONTRIBUTING.md`,
  `RESUME-NEXT.md`, or `SECURITY.md`, run Markdown validation and the aggregate
  gate;
- any other path runs Python quality, coverage, and package/Web asset builds;
- changes under `web/` or `src/kindred/web/`, or to CI/Web build infrastructure
  (`.github/workflows/`, `scripts/build_web.py`, `hatch_build.py`), additionally
  run Web tests and type checking;
- Markdown stored under runtime or package paths, such as a life-asset
  `SKILL.md`, is treated as code rather than documentation;
- a manual CI dispatch runs every gate, while a post-merge `master` push runs
  lightweight link and Python syntax health checks.

For a personal private repository on GitHub Free, the merger must confirm that
the latest `Required checks` run succeeded for the pull request's current head
SHA and selected the expected gates for that diff. A failed, cancelled,
still-running, or incorrectly skipped check is not a pass. Do not push or
force-push directly to `master`, and do not use `[skip ci]` or `skip-checks`
commit trailers. If the post-merge `master` health run fails, stop further
merges and repair or revert it before continuing. When server-side branch
protection is available, prefer enforcing the same rule there.

## Critical Contract Review Checklist

The RS0-T8/RS2-T5 critical-test whitelist is a no-noise boundary. It applies to
deletion, renaming, parameterization, helper extraction, and assertion
rewrites—not only to deleting whole files. Review every change against these
protected sets:

- every assertion and tested variant under `tests/public/`, including future
  public-contract files, is append-only and cannot be replaced;
- every assertion and tested variant in `tests/unit/llm/test_schema_sync.py`;
- every assertion and tested variant in
  `tests/unit/adapters/openclaw/test_hc0_contract.py`;
- every non-prompt security/authorization seed file and pytest node ID listed
  under `critical_tests.security_authorization_semantic_locks` in
  `docs/plans/rs-manifest.yaml`, including registry bypass, secret handling,
  release/promotion, prompt disclosure, and dump-redaction locks;
- every A-class security/authorization semantic lock in
  [`tests/PROMPT-ASSERTS.md`](tests/PROMPT-ASSERTS.md#security-and-authorization),
  backed by `tests/fixtures/prompt_asserts.json` and
  `scripts/ci/check_prompt_asserts.py`.

For a pull request that touches any protected set, reviewers must confirm all
of the following:

1. No assertion or tested variant was removed or weakened. `tests/public/`
   does not accept replacement; its contracts remain append-only.
2. For another protected set, a refactor records the old and replacement
   pytest node IDs in the pull request, plus before/after test counts and
   coverage. A replacement must keep an explicit equivalent semantic
   assertion and may not collapse variants into a broader check.
3. Changes to the A-class snapshot are review-visible. Additions are
   append-only; a wording-driven key replacement must identify the old and new
   keys and preserve the same explicit semantic lock. An existing A key must
   not disappear without that one-to-one evidence or be downgraded.
4. The focused critical suite and the machine-readable prompt census pass:

   ```sh
   uv run --frozen pytest -q \
     tests/public \
     tests/unit/llm/test_schema_sync.py \
     tests/unit/adapters/openclaw/test_hc0_contract.py \
     tests/unit/capability_host/test_registry.py \
     tests/unit/graph/test_act_tool_registry.py \
     tests/unit/graph/test_act_portable_artifact.py \
     tests/unit/test_observability.py \
     tests/unit/config/test_secrets.py \
     tests/unit/scripts/test_public_release.py \
     tests/unit/scripts/test_public_promote.py \
     tests/unit/graph/test_act_skill_render.py::TestRenderActivitySkill::test_possession_context_is_only_disclosed_to_relevant_activity \
     tests/unit/graph/test_expression_context.py::test_writer_gets_one_bounded_expression_context \
     tests/unit/graph/test_sense_l2_prompt.py::TestL2PathWiredThroughImpl::test_recent_life_texture_db_failure_only_omits_section \
     tests/unit/openclaw/test_wire.py::test_wire_rejects_missing_origin_without_echoing_values \
     tests/unit/openclaw/test_wire.py::test_wire_validation_traceback_does_not_echo_private_values \
     tests/unit/graph/test_act_dump.py::test_success_dump_redacts_sensitive_final_json \
     tests/unit/graph/test_act_dump.py::test_tool_loop_error_dump_includes_raw_tool_events_with_secret_keys_redacted
   uv run --frozen python scripts/ci/check_prompt_asserts.py
   ```

5. The pull request's current head passes the normal coverage gate. A green
   aggregate check does not excuse an incorrectly skipped focused suite.

If a protected set is untouched, state that explicitly in the review record.
The canonical file registry remains the `critical_tests` section of
`docs/plans/rs-manifest.yaml`; this checklist defines how reviewers apply it.

## Repository Lifecycle

- Archive milestone plans and gray records within 14 days of GO or merge;
  the discussions index lists only active and recent 30-day entries.
- Keep `RESUME-NEXT.md` within 300 lines by overwriting its fixed template and
  move prior handoffs to `docs/archive/handoffs/`.
- Limit each decision summary to 15 lines, retain the complete ADR rationale,
  link details to a discussion, and place every new decision in a topic group.
- Retire a probe as one script/test/baseline set; move a baseline to
  `tests/fixtures/` first when production tests still read it.
- Preserve structural and security/authorization assertion locks, avoid new
  full-copy assertions for display wording, share new fakes/stubs, and explain
  any test file over 800 lines in the pull request.
- Apply the [critical contract review checklist](#critical-contract-review-checklist)
  to every protected test change.
