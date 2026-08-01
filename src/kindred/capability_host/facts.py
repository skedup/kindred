from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeAlias

from kindred.activity.tool_binding import declared_capability_names_for_activity
from kindred.capability_host.internal import HostTickContext
from kindred_capability_sdk import FactView

FactViewBuilder: TypeAlias = Callable[[HostTickContext], FactView]
ARTIFACT_EXPLICIT_REFS_FACT = "artifact.explicit_refs.v1"
ACTIVITY_CURRENT_FACT = "activity.current"


def anchor_activity_for_act(
    prev_state: Mapping[str, Any] | None,
    act_kind: str | None,
    target: Any,
) -> str | None:
    if act_kind == "start_activity":
        return target.strip() if isinstance(target, str) and target.strip() else None
    activity = (prev_state or {}).get("activity")
    if not isinstance(activity, Mapping):
        return None
    name = activity.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def build_explicit_artifact_refs(context: HostTickContext) -> FactView:
    """投影当前 Activity run 中已经由 T3 持久化的 artifact refs。"""
    artifacts = (
        []
        if context.act_kind == "start_activity"
        else current_activity_artifacts(
            context.next_state,
            context.db,
            activity_name=context.anchor_activity,
        )
    )
    return FactView(
        ARTIFACT_EXPLICIT_REFS_FACT,
        {"artifacts": artifacts},
    )


def current_activity_artifacts(
    state: Mapping[str, Any],
    db: Any,
    *,
    activity_name: str | None = None,
) -> list[dict[str, str]]:
    """返回当前 Activity run 的 committed artifacts 及投递状态。"""
    activity = state.get("activity")
    name = activity.get("name") if isinstance(activity, Mapping) else None
    started_at = activity.get("started_at") if isinstance(activity, Mapping) else None
    if (
        not isinstance(name, str)
        or not isinstance(started_at, str)
        or (activity_name is not None and name != activity_name)
        or db is None
    ):
        return []
    artifacts = db.get_activity_artifacts(activity_name=name, started_at=started_at)
    send_statuses = db.get_activity_send_statuses(
        activity_name=name,
        started_at=started_at,
    )
    return [
        {
            **artifact,
            "status": send_statuses.get(artifact["artifact_ref"], "available"),
        }
        for artifact in artifacts
    ]


def build_current_activity(context: HostTickContext) -> FactView:
    """向 Portable package 投影当前 Activity 的中立声明事实。"""
    capabilities = declared_capability_names_for_activity(
        context.anchor_activity,
        activities_dir=context.activities_dir,
        actions_dir=context.actions_dir,
    )
    activity = context.next_state.get("activity")
    started_at = (
        activity.get("started_at")
        if isinstance(activity, Mapping) and activity.get("name") == context.anchor_activity
        else None
    )
    return FactView(
        ACTIVITY_CURRENT_FACT,
        {
            "name": context.anchor_activity,
            "started_at": started_at if isinstance(started_at, str) else None,
            "capabilities": capabilities,
        },
    )
