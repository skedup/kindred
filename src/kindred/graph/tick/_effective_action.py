"""一次 act proposal 的 effective Action 单一解析入口。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kindred.activity import ActivitySkillError, load_activity_skill
from kindred.graph.tick._act_contract import ActLlmContractError

SETTLE_STEP = "settle"


def resolve_effective_action(
    target: Any,
    kind: Any,
    final_state_diff: dict[str, Any],
    *,
    activities_dir: Path,
    actions_dir: Path,
) -> str | None:
    """解析并校验一次 current_state；end 始终收敛为 settle。"""
    if kind == "end_activity":
        return SETTLE_STEP
    raw = final_state_diff.get("current_state")
    if raw is None:
        raise ActLlmContractError(
            "T2.act.llm: start/advance 必须提交 final_state_diff.current_state"
        )
    if not isinstance(raw, str) or not raw.strip():
        raise ActLlmContractError("T2.act.llm: final_state_diff.current_state 必须是非空字符串")
    if raw == SETTLE_STEP:
        raise ActLlmContractError("T2.act.llm: settle 只能由 end_activity 产生")
    try:
        skill = load_activity_skill(
            target,
            activities_dir=activities_dir,
            actions_dir=actions_dir,
        )
    except ActivitySkillError:
        raise ActLlmContractError(
            "T2.act.llm: 无法解析当前 activity/action package，invalid_paths=['target_activity']"
        ) from None
    if raw not in {use.action for use in skill.uses}:
        raise ActLlmContractError(
            "T2.act.llm: current_state 不属于当前 activity 的 uses，invalid_paths=['current_state']"
        )
    return raw


__all__ = ["SETTLE_STEP", "resolve_effective_action"]
