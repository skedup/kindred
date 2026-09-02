from __future__ import annotations

import json
from pathlib import Path

from kindred.activity.action import list_registered_actions
from kindred.activity.skill import list_registered_activities
from kindred.capability_host.internal import ArtifactProfileRoute
from kindred.capability_host.resources import PackageResources, materialize_runtime_assets
from kindred_capability_compose import create_capability
from kindred_capability_sdk import FactView, InvocationContext, ToolCall, TransientStore


class _Secrets:
    def get(self, _name: str) -> None:
        return None


class _Writer:
    def __init__(self) -> None:
        self.staged: list[tuple[str, dict[str, str | bytes]]] = []

    def stage_bundle(self, profile: str, files: dict[str, str | bytes]) -> None:
        self.staged.append((profile, dict(files)))


def _resource_package(root: Path) -> PackageResources:
    action = root / "actions" / "journal"
    action.mkdir(parents=True)
    (action / "manifest.yaml").write_text(
        "name: journal\n"
        "state_effects:\n"
        "  clarity: {direction: up, magnitude: small}\n"
        "capabilities: [journal]\n",
        encoding="utf-8",
    )
    (action / "SKILL.md").write_text("# journal\n", encoding="utf-8")
    activity = root / "activities" / "reflect"
    activity.mkdir(parents=True)
    (activity / "manifest.yaml").write_text(
        "name: reflect\n"
        "description: keep one thought\n"
        "uses:\n"
        "  - action: journal\n"
        "    intent: write it down\n"
        "terminal_when: the thought is saved\n"
        "duration_hint: short\n"
        "produces: likely\n"
        "requires: []\n",
        encoding="utf-8",
    )
    (activity / "SKILL.md").write_text("# reflect\n", encoding="utf-8")
    (root / "kindred-resources.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_routes": [
                    {
                        "producer_capability": "compose",
                        "selector_capability": "journal",
                        "profile": "example.compose.journal.v1",
                        "member_paths": {"title": "title.txt"},
                        "source_ref_kinds": ["note"],
                    }
                ],
            }
        )
    )
    return PackageResources(
        "journal",
        "kindred-capability-journal",
        "1.0.0",
        root,
        root / "actions",
        root / "activities",
        (
            ArtifactProfileRoute(
                "compose",
                "journal",
                "example.compose.journal.v1",
                {"title": "title.txt"},
                frozenset({"note"}),
            ),
        ),
    )


def test_package_resources_extend_life_assets_without_replacing_core(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    view = materialize_runtime_assets(
        runtime_root,
        resources=(_resource_package(tmp_path / "package"),),
        kindred_version="0.4.0",
    )
    actions = list_registered_actions(actions_dir=view.actions_dir)
    activities = list_registered_activities(activities_dir=view.activities_dir)

    assert "journal" in actions
    assert "compose" in actions
    assert "reflect" in activities
    assert "reach_out_to_user" in activities


def test_compose_consumes_a_generic_package_profile_route() -> None:
    writer = _Writer()
    contribution = create_capability(settings={}, secrets=_Secrets())
    context = InvocationContext(
        tick_id=1,
        triggered_at=None,
        facts=(
            FactView("activity.current", {"capabilities": ("compose", "journal")}),
            FactView(
                "artifact.profile_routes",
                {
                    "routes": (
                        {
                            "selector_capability": "journal",
                            "profile": "example.compose.journal.v1",
                            "member_paths": {"title": "title.txt"},
                            "source_ref_kinds": ("note",),
                        },
                    )
                },
            ),
        ),
        transient=TransientStore(),
        artifact_writer=writer,
    )

    result = contribution.tool_bindings[0].handler(
        ToolCall("write_compose", {"title": "A title", "content": "A body"}),
        context,
    )

    assert result.tool_result.is_error is False
    assert writer.staged == [
        ("example.compose.journal.v1", {"content.md": "A body", "title.txt": "A title"})
    ]
