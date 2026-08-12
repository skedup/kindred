"""原子动作库（actions/）—— 全局动作节点池：解析 + 模型 + 加载。

feat/atomic-actions-rest（2026-06-19）
=====================================

**原子动作 = 全局节点池**（定义一次，被任意 activity 复用）。判据（skedush）：
能被**一个 spirit 动画**体现的原子行为，就构成一个原子动作（= 状态机节点 =
未来动画名）。例：walk / eat / sleep / makeup。

与 ``ActivitySkill`` 的三层分工（见 docs/discussions/2026-06-18-atomic-actions.md §3/§4）：

- **原子动作**（本模块）= 客观行为 + 基准 effect + 客观软前提（``applies_when``）
- **``activity.uses[].intent``** = 染色（动作在某 activity 语境的味道/时机）
- **``activity.method``** = 更高层整体编排哲学

原子动作**不带终态语义**——同一个 sleep，在 rest 里是收尾点，在别的 activity
里可能是中途。终态属状态机(activity 的 ``terminal_when``)，不属节点(动作)。

本模块只做 **read + 解析 + 校验**，不消费 state_effects 算状态（derive/act 的事）。
复用 ``skill.py`` 的 ``StateEffect`` / ``VALID_EFFECT_KEYS`` / name 安全正则——
原子动作与 activity 的 effect 词表、name 规则同源，不另起一套。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator

from kindred.activity.package import (
    ActivityPackageError,
    list_registered_packages,
    load_package_parts,
)
from kindred.activity.skill import (
    _SAFE_ACTIVITY_NAME,
    VALID_EFFECT_KEYS,
    ActivitySkill,
    ActivitySkillError,
    ActivityUse,
    StateEffect,
)
from kindred.life_assets import ACTIONS_DIR
from kindred.state._base import StrictBase
from kindred.state._types import NonBlankStr

# ─────────────────────────────────────────────────────────────────────
# 模型
# ─────────────────────────────────────────────────────────────────────


class PlaceSlot(StrictBase):
    """action-local 地点槽位契约。

    ``needed_for`` 描述这个槽位服务于什么动作语境；它不是 action 级可用前提，
    避免和 ``AtomicAction.applies_when`` 混淆。
    """

    needed_for: NonBlankStr = Field(description="这个地点槽位服务的动作语境")
    description: NonBlankStr = Field(description="这个地点在动作中的自然语言含义")


class AtomicAction(StrictBase):
    """一个全局原子动作（状态机节点 / spirit 动画）。

    ``name`` = 目录名 = 未来 spirit 动画名（原子动作判据：能用一个动画展示）。
    ``state_effects`` 是通常体验软先验，不由 Host 自动兑现或限制本次 signed delta。
    ``applies_when`` 是软前提（自然语言；None/省略=任意情境）——LLM 仲裁能否转
    到此动作的判断依据（选项 C：声明式软前提 + LLM 仲裁，不写死硬转移边）。
    """

    name: NonBlankStr = Field(description="原子动作名（= 目录名 = spirit 动画名）")
    desc: str | None = Field(default=None, description="这个动作的具体描述")
    state_effects: dict[str, StateEffect] = Field(
        description="该动作通常体验的方向与档位软先验，供心结合本次经历解释"
    )
    applies_when: str | None = Field(
        default=None,
        description="软前提（自然语言）；None/省略=任意情境可开始",
    )
    place_slots: dict[str, PlaceSlot] = Field(
        default_factory=dict,
        description="action-local 地点槽位声明；key 形成 <action>.<slot_id> 契约",
    )
    capabilities: list[NonBlankStr] = Field(
        default_factory=list,
        description="该原子动作绑定的 capability 名；省略表示不绑定外部能力",
    )

    @model_validator(mode="after")
    def _check_effects(self) -> AtomicAction:
        if not self.state_effects:
            raise ValueError(
                f"action {self.name!r}: 必须声明非空 state_effects（原子动作是状态影响来源）"
            )
        unknown = set(self.state_effects) - VALID_EFFECT_KEYS
        if unknown:
            raise ValueError(
                f"action {self.name!r}: 未知 state_effect key {sorted(unknown)}；"
                f"合法 key 限 Needs+Affect：{sorted(VALID_EFFECT_KEYS)}"
            )
        return self

    @model_validator(mode="after")
    def _check_place_slot_keys(self) -> AtomicAction:
        for slot_id in self.place_slots:
            if not _SAFE_ACTIVITY_NAME.match(slot_id):
                raise ValueError(
                    f"action {self.name!r}: place_slots key={slot_id!r} 非法"
                    "（须匹配 ^[a-z][a-z0-9_]*$，拒 / 与 ..）"
                )
        return self

    @model_validator(mode="after")
    def _check_capability_names(self) -> AtomicAction:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for capability_name in self.capabilities:
            if not _SAFE_ACTIVITY_NAME.match(capability_name):
                raise ValueError(
                    f"action {self.name!r}: capabilities name={capability_name!r} 非法"
                    "（须匹配 ^[a-z][a-z0-9_]*$，拒 / 与 ..）"
                )
            if capability_name in seen:
                duplicates.add(capability_name)
            seen.add(capability_name)
        if duplicates:
            raise ValueError(
                f"action {self.name!r}: capabilities 中能力名重复 {sorted(duplicates)}"
            )
        return self


# ─────────────────────────────────────────────────────────────────────
# 加载（复用 skill.py 的 name 正则，契约同源）
# ─────────────────────────────────────────────────────────────────────


def is_valid_action_name(name: str) -> bool:
    """action 名是否合法（``^[a-z][a-z0-9_]*$``，拒 ``/`` 与 ``..``）。

    与 activity name 规则同源（复用 ``_SAFE_ACTIVITY_NAME``）——动作与活动的
    name 安全契约一致，不另立一套正则。
    """
    return bool(_SAFE_ACTIVITY_NAME.match(name))


def list_registered_actions(*, actions_dir: Path = ACTIONS_DIR) -> list[str]:
    """枚举 ``actions_dir`` 下所有已注册的原子动作名（排序后返回）。

    「已注册」= 目录名匹配 name 正则且其下同时有 ``manifest.yaml`` 和 ``SKILL.md``。
    目录不存在/空 → 返空 list（不 raise；运行时数据问题，调用方降级）。
    非法名/包文件不完整/普通文件都跳过。
    """
    return list_registered_packages(packages_dir=actions_dir)


def load_atomic_action(name: str, *, actions_dir: Path = ACTIONS_DIR) -> AtomicAction:
    """加载 ``<actions_dir>/<name>/{manifest.yaml,SKILL.md}`` → ``AtomicAction``。

    找不到 / manifest 坏 / 校验失败都 raise ``ActivitySkillError``（与 activity
    同款异常类型，调用方统一 catch）。manifest ``name`` 必须 == 目录名。与
    activity 不同：原子动作**没有 method body**（动作是客观节点，编排哲学属
    activity）——``SKILL.md`` 仅作自然语言补充，不映射到模型字段。
    """
    try:
        parts = load_package_parts(
            packages_dir=actions_dir,
            name=name,
            package_kind="action",
        )
    except ActivityPackageError as exc:
        raise ActivitySkillError(str(exc)) from exc
    if "tools" in parts.manifest:
        raise ActivitySkillError(
            f"{parts.manifest_path}: action manifest 旧字段 tools 已不支持；请改用 capabilities"
        )
    try:
        action = AtomicAction.model_validate(parts.manifest)
    except Exception as exc:  # pydantic ValidationError / ValueError
        raise ActivitySkillError(f"{parts.manifest_path}: action manifest 校验失败：{exc}") from exc
    if action.name != name:
        raise ActivitySkillError(
            f"{parts.manifest_path}: manifest name={action.name!r} 与目录名 {name!r} 不一致"
        )
    return action


# ─────────────────────────────────────────────────────────────
# effect resolve（baseline + per-key override merge）
# ─────────────────────────────────────────────────────────────


def _find_use(skill: ActivitySkill, step: str) -> ActivityUse | None:
    """在 skill.uses 里找 action==step 的那条 use（step 是 activity 内的原子动作名）。"""
    return next((u for u in skill.uses if u.action == step), None)


def resolve_step_effects(
    skill: ActivitySkill,
    step: str,
    *,
    actions_dir: Path = ACTIONS_DIR,
) -> dict[str, StateEffect]:
    """解析 ``(activity, step)`` 生效 effect = action baseline + uses override per-key merge。

    步骤（docs/discussions/2026-06-18 §3/§4，skedush 6/19 拍）：
    1. step 必须 ∈ ``skill.uses`` 的 action 名（**activity 限定**，不是全量池）。
       不在 → 返空 dict（调用方降级：原样写不 clamp）。
    2. 加载全局 action SKILL 取 baseline state_effects（加载失败 → 仅用 override，
       降级不 raise：运行时数据问题不该崩 tick）。
    3. per-key merge：override 命中的 key 覆盖 baseline，未命中 fallback baseline。

    返回生效 ``{need/affect 名: StateEffect}``。空 dict = 该 step 无任何 effect。
    """
    use = _find_use(skill, step)
    if use is None:
        # step 不在本 activity 的 uses 里——activity 限定外，调用方降级。
        return {}
    # baseline：从全局 action 库取（加载失败降级为空 baseline，仅用 override）。
    baseline: dict[str, StateEffect] = {}
    try:
        baseline = dict(load_atomic_action(use.action, actions_dir=actions_dir).state_effects)
    except ActivitySkillError:
        # action SKILL 缺失/坏——降级：仅用 override（可能也空）。调用方原样写不 clamp。
        pass
    # per-key merge：override 覆盖同名 key，其余 fallback baseline。
    merged = dict(baseline)
    merged.update(use.state_effects)
    return merged


__all__ = [
    "AtomicAction",
    "PlaceSlot",
    "is_valid_action_name",
    "list_registered_actions",
    "load_atomic_action",
    "resolve_step_effects",
]
