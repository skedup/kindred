"""上一拍 Action entry 的稳定体验档位与 Sense 安全投影。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from kindred.activity import ActivitySkillError, load_activity_skill, load_atomic_action

Status = Literal["present", "not_entry", "invalid"]
ActionEntry = tuple[int, str, str, str, dict[str, Any]]
ActionResultGrade = tuple[Status, str | None, str | None, str, tuple[str, ...]]


def build_action_result_grade(
    recent_ticks: list[dict[str, Any]],
    *,
    activities_dir: Path,
    actions_dir: Path,
) -> ActionResultGrade:
    """从最近 canonical ticks 构造单次、非持久化的 Action 体验提示。"""
    entry, status = _derive_entry(recent_ticks)
    if entry is None:
        return status, None, None, "", ()
    _, activity_name, _, action_step, act_result = entry
    try:
        skill = load_activity_skill(
            activity_name, activities_dir=activities_dir, actions_dir=actions_dir
        )
        matches = [use for use in skill.uses if use.action == action_step]
        if len(matches) != 1:
            return "invalid", action_step, None, "", ()
        action = load_atomic_action(action_step, actions_dir=actions_dir)
    except (ActivitySkillError, OSError):
        return "invalid", action_step, None, "", ()

    grade = _grade_for_entry(entry)
    facts, fact_kinds = _summarize_facts(act_result)
    lines = [f"上一拍刚进入 Action：{action.name}"]
    if action.desc:
        lines.append(f"动作含义：{action.desc}")
    lines.extend(
        (
            f"这一步在当前 Activity 中的意图：{matches[0].intent}",
            f"已确认的结构化事实：{'；'.join(facts) if facts else '无额外结构化结果'}",
            f"Action 体验结果档位：{grade}",
        )
    )
    return "present", action_step, grade, "\n".join(lines), fact_kinds


def _derive_entry(recent_ticks: list[dict[str, Any]]) -> tuple[ActionEntry | None, Status]:
    if not recent_ticks or not isinstance(recent_ticks[0], dict):
        return None, "not_entry"
    latest = recent_ticks[0]
    result = latest.get("act_result")
    if not isinstance(result, dict) or result.get("committed") is not True:
        return None, "not_entry"
    kind = result.get("kind")
    if kind not in {"start_activity", "advance_activity"}:
        return None, "not_entry"
    tick_id = latest.get("id")
    if not isinstance(tick_id, int) or isinstance(tick_id, bool):
        return None, "invalid"
    identity = _activity_identity(latest.get("activity"))
    if identity is None or identity[2] == "settle" or result.get("current_state") != identity[2]:
        return None, "invalid"
    if kind == "advance_activity":
        if len(recent_ticks) < 2 or not isinstance(recent_ticks[1], dict):
            return None, "not_entry"
        previous = _activity_identity(recent_ticks[1].get("activity"))
        if previous is None:
            return None, "invalid"
        if previous == identity:
            return None, "not_entry"
    return (tick_id, *identity, result), "present"


def _activity_identity(raw: Any) -> tuple[str, str, str] | None:
    if not isinstance(raw, dict):
        return None
    values = (raw.get("name"), raw.get("started_at"), raw.get("step"))
    if not all(isinstance(value, str) and value for value in values):
        return None
    return values  # type: ignore[return-value]


def _grade_for_entry(entry: ActionEntry) -> str:
    encoded = json.dumps(
        entry[:4],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    bucket = int.from_bytes(hashlib.sha256(encoded).digest(), byteorder="big", signed=False) % 100
    return _grade_for_bucket(bucket)


def _grade_for_bucket(bucket: int) -> str:
    return next(
        grade
        for boundary, grade in ((5, "A"), (25, "B"), (75, "C"), (95, "D"), (100, "E"))
        if bucket < boundary
    )


def _summarize_facts(result: dict[str, Any]) -> tuple[list[str], tuple[str, ...]]:
    facts: list[str] = []
    kinds: list[str] = []
    traces = result.get("tool_trace")
    if isinstance(traces, list):
        for trace in traces:
            if not isinstance(trace, dict) or not isinstance(trace.get("tool"), str):
                continue
            ok = trace.get("ok")
            if not isinstance(ok, bool):
                continue
            fact = f"工具 {trace['tool']}{'成功' if ok else '失败'}"
            response = trace.get("result")
            error_type = response.get("error_type") if isinstance(response, dict) else None
            if not ok and isinstance(error_type, str):
                fact += f"（error_type={error_type}）"
            facts.append(fact)
            kinds.append("tool")
    delivery = result.get("outbound_delivery")
    if isinstance(delivery, dict) and isinstance(delivery.get("delivered"), bool):
        facts.append("消息已投递" if delivery["delivered"] else "消息未投递")
        kinds.append("outbound_delivery")
    artifacts = result.get("artifacts")
    if isinstance(artifacts, list):
        profiles = [item["profile"] for item in artifacts if _has_string(item, "profile")]
        if profiles:
            facts.append(f"已提交 Artifact {len(profiles)} 个（profile={','.join(profiles)}）")
            kinds.append("artifacts")
    for key, text in (
        ("destination_choice", "地点已选择"),
        ("destination_abandoned", "地点已放弃"),
        ("location_arrival", "地点已到达"),
    ):
        if isinstance(result.get(key), dict):
            facts.append(text)
            kinds.append(key)
    return facts, tuple(dict.fromkeys(kinds))


def _has_string(raw: Any, key: str) -> bool:
    return isinstance(raw, dict) and isinstance(raw.get(key), str)
