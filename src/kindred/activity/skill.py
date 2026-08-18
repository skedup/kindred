"""Activity package 标准——模型 + 加载。

**当前标准（2026-06-18 原子动作重构）**
=====================================

**activity = 高级意图 / 状态机**（认知层，心选择）。activity 不再内含写死的
``states[]``，而是通过 ``uses[]`` 声明它由哪几个**全局原子动作**（actions/）
组成（状态机节点），并用 ``terminal_when`` 声明状态机终止软条件。

- **原子动作**（``kindred.activity.action.AtomicAction``）= 客观行为 + 基准 effect +
  客观软前提（``applies_when``），全局复用（``actions/<name>/manifest.yaml``）。
- **uses[] 条目**（``ActivityUse``）= 引用一个原子动作 + ``intent`` 染色 +
  可选 per-key ``state_effects`` override。
- step 只能从 ``uses`` 的 action 名里选；end_activity 转进框架隐式谢幕终态 settle。

判断标准（skedush 6/08）：

> 能用一个 **spirit 动画**展示的原子行为 = 原子动作（拍照/化妆/晒太阳/走/吃）；
> 得靠一串原子动作组合达成的 = 高级意图（explore_food / dine_out）。

**权威**：结构见 ``2026-06-18-atomic-actions.md``；上文“基准 effect”在 LD4 中仅指通常体验软先验，
当前 effect 见：
``docs/archive/discussions/2026-08/2026-08-04-experience-appraisal-and-descriptive-interior.md``
docs/15 archived。

本模块只做 **read + 解析 + 校验**；state_effects 生效见 ``resolve_step_effects``，
状态推进见 ``graph/tick/act_llm.py``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from kindred.activity.package import (
    ActivityPackageError,
    list_registered_packages,
    load_package_parts,
)
from kindred.life_assets import ACTIONS_DIR, ACTIVITIES_DIR
from kindred.location.models import MAX_LOCATION_CANDIDATES, MAX_LOCATION_RADIUS_KM
from kindred.state._base import StrictBase
from kindred.state._types import SAFE_NAME_PATTERN, NonBlankStr
from kindred.state.interior import Affect, Needs

# ─────────────────────────────────────────────────────────────────────
# Literal 词表（docs/15 §3/§4）
# ─────────────────────────────────────────────────────────────────────

Direction = Literal["up", "down"]
Magnitude = Literal["large", "medium", "small"]
DurationHint = Literal["short", "medium", "long"]
Produces = Literal["always", "likely", "optional"]

MAGNITUDE_RANGE: dict[str, tuple[int, int]] = {
    "large": (25, 40),
    "medium": (10, 25),
    "small": (3, 10),
}

# 向后兼容别名（earlier milestone 只文档化时叫 MAGNITUDE_REFERENCE）。
MAGNITUDE_REFERENCE = MAGNITUDE_RANGE

# state_effects 合法 key：绑定 Needs + Affect 字段，typo key 立即报错，
# 不留到 derive 消费时静默丢弃。
VALID_EFFECT_KEYS: frozenset[str] = frozenset(Needs.model_fields) | frozenset(Affect.model_fields)

# 安全 activity 名：单段小写，拒 / 与 ..（防 path traversal）。
# 真相源在 state/_types.SAFE_NAME_PATTERN（earlier review N-1 后与 ActivityContext.destinations
# key 共用同一条规则）；本别名保持既有引用点不动。
_SAFE_ACTIVITY_NAME = SAFE_NAME_PATTERN


def is_valid_activity_name(name: str) -> bool:
    """activity 名是否合法（``^[a-z][a-z0-9_]*$``，拒 ``/`` 与 ``..``）。

    单一真相源——供 dream land（_land_fs）等模块复用同一套 name 安全规则，
    避免各处重复正则导致契约漂移。
    """
    return bool(_SAFE_ACTIVITY_NAME.match(name))


# ─────────────────────────────────────────────────────────────────────
# 异常
# ─────────────────────────────────────────────────────────────────────


class ActivitySkillError(RuntimeError):
    """activity SKILL 解析 / 加载 / 校验错。"""


# ─────────────────────────────────────────────────────────────────────
# 模型
# ─────────────────────────────────────────────────────────────────────


class StateEffect(StrictBase):
    """单个 need/affect 的通常体验方向与档位软先验。docs/15 §3。"""

    direction: Direction
    magnitude: Magnitude


class LocationBinding(StrictBase):
    """activity 级地点查询语义。

    action 只声明 action-local slot；activity 用 binding 说明“这次要找什么样的地方”。
    Provider 查询会在未来 pre-act 阶段按 binding 组装，这里只落 schema 和校验。
    """

    query: NonBlankStr = Field(description="Provider 查询的自然语言地点意图")
    categories: list[NonBlankStr] = Field(
        default_factory=list,
        description="Provider category hints；为空表示不限定",
    )
    radius_min_km: float | None = Field(
        default=None,
        description="查询半径下界；None 表示由 config / provider 默认补齐",
    )
    radius_max_km: float | None = Field(
        default=None,
        description="查询半径上界；None 表示由 config / provider 默认补齐",
    )
    limit: int | None = Field(
        default=None,
        description="候选数量上限；None 表示由 config / provider 默认补齐",
    )

    @model_validator(mode="after")
    def _check_ranges(self) -> LocationBinding:
        if self.radius_min_km is not None and self.radius_min_km < 0:
            raise ValueError("radius_min_km must be >= 0")
        if self.radius_max_km is not None and self.radius_max_km < 0:
            raise ValueError("radius_max_km must be >= 0")
        if self.radius_min_km is not None and self.radius_min_km > MAX_LOCATION_RADIUS_KM:
            raise ValueError(f"radius_min_km must be <= {MAX_LOCATION_RADIUS_KM:g}")
        if self.radius_max_km is not None and self.radius_max_km > MAX_LOCATION_RADIUS_KM:
            raise ValueError(f"radius_max_km must be <= {MAX_LOCATION_RADIUS_KM:g}")
        if (
            self.radius_min_km is not None
            and self.radius_max_km is not None
            and self.radius_min_km > self.radius_max_km
        ):
            raise ValueError("radius_min_km must be <= radius_max_km")
        if self.limit is not None and self.limit <= 0:
            raise ValueError("limit must be > 0")
        if self.limit is not None and self.limit > MAX_LOCATION_CANDIDATES:
            raise ValueError(f"limit must be <= {MAX_LOCATION_CANDIDATES}")
        return self


class ActivityUse(StrictBase):
    """activity 引用的一个原子动作（uses[] 条目）。

    三层分工（docs/discussions/2026-06-18 §3）：
    - ``action``：引用全局原子动作名（effect baseline 在 actions/<action>/manifest.yaml）。
    - ``intent``：**必填染色**——这个动作在本 activity 语境里的味道/时机。
      同一个 walk，在 dine_out 是「走向那家店、带着期待」，在 rest 是「拖着疲惫回家」。
    - ``bind_places``：把该 action 的 place slot 绑定到本 activity 的 location binding。
    - ``state_effects``：可选 **per-key override**——本 activity 里这个动作的 effect 特化。
      命中的 key 覆盖 action baseline，未命中的 key fallback baseline（DRY）。
    """

    action: NonBlankStr = Field(description="引用的全局原子动作名")
    intent: NonBlankStr = Field(description="染色：该动作在本 activity 语境的味道/时机（必填）")
    state_effects: dict[str, StateEffect] = Field(
        default_factory=dict,
        description="可选 per-key override：覆盖 action baseline 的那几个 effect key",
    )
    bind_places: dict[str, NonBlankStr] = Field(
        default_factory=dict,
        description="action-local place slot id → activity location binding id",
    )

    @model_validator(mode="after")
    def _check_action_name(self) -> ActivityUse:
        # N-3：action 名必须过 name 安全正则（^[a-z][a-z0-9_]*$，拒 / 与 ..）。
        # 否则坑值如 "../walk" 会被当合法 step 落库，后续 resolve_step_effects
        # 拿它去读 action 文件静默降级，成为隐患。与 action.is_valid_action_name
        # 同源正则（skill._SAFE_ACTIVITY_NAME）；不反向 import 避免循环依赖。
        if not _SAFE_ACTIVITY_NAME.match(self.action):
            raise ValueError(
                f"use action={self.action!r}: 非法动作名（须匹配 ^[a-z][a-z0-9_]*$，拒 / 与 ..）"
            )
        return self

    @model_validator(mode="after")
    def _check_bind_places_names(self) -> ActivityUse:
        for slot_id, binding_id in self.bind_places.items():
            if not _SAFE_ACTIVITY_NAME.match(slot_id):
                raise ValueError(
                    f"use action={self.action!r}: bind_places slot={slot_id!r} 非法"
                    "（须匹配 ^[a-z][a-z0-9_]*$，拒 / 与 ..）"
                )
            if not _SAFE_ACTIVITY_NAME.match(binding_id):
                raise ValueError(
                    f"use action={self.action!r}: bind_places binding={binding_id!r} 非法"
                    "（须匹配 ^[a-z][a-z0-9_]*$，拒 / 与 ..）"
                )
        return self

    @model_validator(mode="after")
    def _check_override_keys(self) -> ActivityUse:
        # override 可空（纯用 baseline）；若给了，key 必须是合法 effect 字段。
        unknown = set(self.state_effects) - VALID_EFFECT_KEYS
        if unknown:
            raise ValueError(
                f"use action={self.action!r}: 未知 override state_effect key {sorted(unknown)}；"
                f"合法 key 限 Needs+Affect：{sorted(VALID_EFFECT_KEYS)}"
            )
        return self


class ActivitySkill(StrictBase):
    """一个高级意图 activity（认知层，心选择）。docs/discussions/2026-06-18。

    activity = 状态机：``uses`` 声明它由哪几个原子动作（状态机节点）组成。
    心在 act 时决定当前处于哪个动作（step ∈ uses 的 action 名）。effect 取值：
    action/uses 合并结果只提供通常体验软先验，不约束本次 signed delta。
    ``terminal_when`` 是状态机终止软条件（自然语言，心仲裁何时 end_activity）。
    """

    name: NonBlankStr = Field(description="唯一名，= 目录名")
    description: NonBlankStr = Field(description="一句话，给 sense.llm 挑选用")
    location_bindings: dict[str, LocationBinding] = Field(
        default_factory=dict,
        description="activity 级地点查询语义；uses[].bind_places 按 id 引用",
    )
    uses: list[ActivityUse] = Field(
        description="本 activity 由哪几个原子动作组成（状态机节点；step 只能从这选）"
    )
    terminal_when: NonBlankStr = Field(
        description="状态机终止软条件（自然语言，心仲裁何时 end_activity）"
    )
    duration_hint: DurationHint = Field(description="软参考，心决定继续/收尾")
    produces: Produces = Field(description="产出落盘倾向 always/likely/optional")
    requires: list[str] = Field(default_factory=list, description="bag 物品/前置")
    method: NonBlankStr = Field(description="body 自然语言工具箱（整体编排哲学）")

    @model_validator(mode="after")
    def _check_uses_non_empty(self) -> ActivitySkill:
        if not self.uses:
            raise ValueError(f"activity {self.name!r}: 必须声明至少一个 use（原子动作）")
        return self

    @model_validator(mode="after")
    def _check_uses_action_unique(self) -> ActivitySkill:
        # N-3：uses 里 action 名必须唯一。重复会让 _find_use first-match，
        # 同一 step 多个 intent/override 不可区分（哪个生效静默依赖顺序）。
        seen: set[str] = set()
        dups: list[str] = []
        for u in self.uses:
            if u.action in seen:
                dups.append(u.action)
            seen.add(u.action)
        if dups:
            raise ValueError(
                f"activity {self.name!r}: uses 中 action 名重复 {sorted(set(dups))}；"
                f"同一原子动作只能出现一次"
            )
        return self

    @model_validator(mode="after")
    def _check_location_binding_refs(self) -> ActivitySkill:
        for binding_id in self.location_bindings:
            if not _SAFE_ACTIVITY_NAME.match(binding_id):
                raise ValueError(
                    f"activity {self.name!r}: location_bindings key={binding_id!r} 非法"
                    "（须匹配 ^[a-z][a-z0-9_]*$，拒 / 与 ..）"
                )
        declared = set(self.location_bindings)
        for u in self.uses:
            for slot_id, binding_id in u.bind_places.items():
                if binding_id not in declared:
                    raise ValueError(
                        f"activity {self.name!r}: use action={u.action!r} bind_places "
                        f"{slot_id!r}->{binding_id!r} 未在 location_bindings 中声明"
                    )
        return self


# ─────────────────────────────────────────────────────────────────────
# 加载
# ─────────────────────────────────────────────────────────────────────


def list_registered_activities(*, activities_dir: Path = ACTIVITIES_DIR) -> list[str]:
    """枚举 ``activities_dir`` 下所有已注册的 activity 名（排序后返回）。

    「已注册」= 目录名匹配 ``_SAFE_ACTIVITY_NAME`` 且其下同时有 ``manifest.yaml`` 和
    ``SKILL.md`` 文件。
    用于给 sense.llm prompt 注入「可选活动清单」——把 target_activity 从
    「自由发挥」收成「只能从注册名里选（explore_food）」。

    目录不存在 / 为空 → 返空 list（不 raise；运行时数据问题，调用方降级处理）。
    非法名目录、包文件不完整的目录、普通文件都跳过（不当作注册活动）。
    """
    return list_registered_packages(packages_dir=activities_dir)


def load_activity_skill(
    name: str,
    *,
    activities_dir: Path = ACTIVITIES_DIR,
    actions_dir: Path = ACTIONS_DIR,
) -> ActivitySkill:
    """加载 ``<activities_dir>/<name>/{manifest.yaml,SKILL.md}`` → ``ActivitySkill``。

    找不到文件 / manifest 坏 / 校验失败都 raise ``ActivitySkillError``。
    调用方决定 raise 还是降级（act.llm 对缺失 SKILL 降级，见节点）。
    """
    try:
        parts = load_package_parts(
            packages_dir=activities_dir,
            name=name,
            package_kind="activity",
        )
    except ActivityPackageError as exc:
        raise ActivitySkillError(str(exc)) from exc
    meta = dict(parts.manifest)
    if "method" in meta:
        raise ActivitySkillError(
            f"{parts.manifest_path}: method 属于 SKILL.md 正文，不允许放在 manifest.yaml"
        )
    meta["method"] = parts.skill_body
    try:
        skill = ActivitySkill.model_validate(meta)
    except Exception as exc:  # pydantic ValidationError / ValueError
        raise ActivitySkillError(
            f"{parts.manifest_path}: activity manifest 校验失败：{exc}"
        ) from exc
    # manifest name 必须 == 目录名（防 explore_food/manifest.yaml 写 name: sleep）
    if skill.name != name:
        raise ActivitySkillError(
            f"{parts.manifest_path}: manifest name={skill.name!r} 与目录名 {name!r} 不一致"
        )
    _validate_bind_places_against_actions(
        skill, actions_dir=actions_dir, source=str(parts.manifest_path)
    )
    return skill


def _validate_bind_places_against_actions(
    skill: ActivitySkill, *, actions_dir: Path, source: str
) -> None:
    """校验 ``uses[].bind_places`` 的 key 命中对应 action 的 ``place_slots``。

    这一步需要读 action SKILL，所以放在 load 阶段；ActivitySkill 纯模型只校验自身
    的 binding id 是否存在，避免模块顶层互相 import 形成循环依赖。
    """

    for use in skill.uses:
        if not use.bind_places:
            continue
        from kindred.activity.action import load_atomic_action

        try:
            action = load_atomic_action(use.action, actions_dir=actions_dir)
        except ActivitySkillError as exc:
            raise ActivitySkillError(
                f"{source}: use action={use.action!r} 声明 bind_places，"
                f"但无法加载对应 action SKILL 进行校验：{exc}"
            ) from exc
        known_slots = set(action.place_slots)
        unknown_slots = set(use.bind_places) - known_slots
        if unknown_slots:
            raise ActivitySkillError(
                f"{source}: use action={use.action!r} bind_places slot "
                f"{sorted(unknown_slots)} 不在 action place_slots {sorted(known_slots)} 中"
            )


__all__ = [
    "MAGNITUDE_RANGE",
    "MAGNITUDE_REFERENCE",
    "ActivitySkill",
    "ActivitySkillError",
    "ActivityUse",
    "LocationBinding",
    "StateEffect",
    "list_registered_activities",
    "load_activity_skill",
]
