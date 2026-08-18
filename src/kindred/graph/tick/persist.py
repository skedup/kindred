"""``T3.persist.{write_state, write_memory, flush_bundle}`` —— 串行三个写节点。

参考文档：14-heart-graph.md §3.5 / §3.6 / §3.7 + §2.2.2 (拆分原则) +
§2.3.3 (T3 串行而非并行) + 09-memory.md §4 (bundle 渲染)

职责：

- ``write_state``：``KindredDB.transaction()`` + ``insert_tick(TickWriteParams)``
  单事务原子写 ``tick`` 表 +（``significance >= 7`` 时）``episode_recall``。
  rowid 写回 ``state["tick_id"]`` 给后两节点用。
- ``write_memory``：三元组 diff 找出本 tick 新增的 thoughts，单事务批量 INSERT。
- ``flush_bundle``：整文重写 ``context-bundle.md`` = NOW 段（next_state）+ Layer A/B/C
  （read-time 渲染，见 ``_bundle_layers``，逐段独立兜底 §4.5）；atomic ``tmpfile + rename``。
  Layer C 走 ``episode JOIN episode_recall``（cooldown>0），写成后衰减选中高光 cooldown
  （§4.4.4.2 F-1 防御）——故本节点对 DB 有一处写（容错，失败不崩 tick）。

factory 模式注入 deps
=====================

LangGraph 节点签名硬约束 ``(state) -> dict``，而 T3 三节点都需要 db 实例 +
bundle 路径。:func:`make_persist_nodes` 接收 deps，返回 closure 函数 dict；
``build_tick_graph(...)`` 注入到 graph。

真逻辑写在顶层 ``_*_impl(deps, state)``，``_make_*(deps)`` 是 thin wrapper
closure 只负责绑 deps。收益：IDE/mypy 可直跳真实现、测试可直接
``_write_state_impl(fake_deps, state)`` 跳过 factory 创建链。

（对比备选：state 携带 db 违反 LangGraph TypedDict 可序列化前提；模块级
provider 是隐式全局，调试不友好。故选 factory closure。）

为什么三个独立节点（失败语义不同 = 才能独立 retry / fallback / 告警）：

- ``write_state`` 失败必须中断本轮 tick（next_state 不写主表，下一 tick 仍从
  prev_state 起，避免半状态）
- ``write_memory`` 可容忍（下次 tick 重试）
- ``flush_bundle`` 可容忍（bundle 是次要派生，可从 state 重建）

为什么串行：state 主表先落（真相源）→ memory 后补（可从 state 重生）→
bundle 最后（二阶派生，read state 主表才能刷完整）。
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from kindred.db._types import TickWriteParams
from kindred.graph._shared._common import (
    NodeReturn,
    validate_state,
    validate_thoughts,
)
from kindred.graph.tick import _bundle_layers as _layers
from kindred.graph.tick._possession_narrative import current_possession_fact_lines
from kindred.relationship import render_relationship_summary
from kindred.relationship.models import RelationshipChange
from kindred.relationship.preflight import RelationshipReader, require_user_relationship
from kindred.state.state import State
from kindred.state.tick import TickState

if TYPE_CHECKING:
    from collections.abc import Callable

    from kindred.db.facade import KindredDB
    from kindred.state.outward import Environment


_LOG = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Deps + factory
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PersistDeps:
    """T3 三节点共享的运行时依赖。

    - ``db``：已 open 的 :class:`KindredDB` 实例（generally daemon-scoped）
    - ``bundle_path``：嘴侧 bundle 文件路径（09 §4.2 沿用 first-party predecessor）
    """

    db: KindredDB
    bundle_path: Path
    relationship_reader: RelationshipReader | None = None


def make_persist_nodes(
    db: KindredDB,
    bundle_path: Path,
    *,
    relationship_reader: RelationshipReader | None = None,
) -> dict[str, Callable[[TickState], NodeReturn]]:
    """构造 T3 三节点 closure dict。

    用法（详 build.py / cli.py）::

        deps_db = KindredDB.open(db_path)
        with deps_db as db:
            nodes = make_persist_nodes(db, bundle_path)
            graph = build_tick_graph(persist_nodes=nodes)
            graph.invoke(initial_state)

    返回 key 与 14 §2.2 节点常量对齐：
    ``"T3.persist.write_state"`` / ``"T3.persist.write_memory"``
    / ``"T3.persist.flush_bundle"``。
    """
    deps = PersistDeps(
        db=db,
        bundle_path=bundle_path,
        relationship_reader=relationship_reader,
    )
    return {
        "T3.persist.write_state": _make_write_state(deps),
        "T3.persist.write_memory": _make_write_memory(deps),
        "T3.persist.flush_bundle": _make_flush_bundle(deps),
    }


# ─────────────────────────────────────────────────────────────────────
# 公共 helper
# ─────────────────────────────────────────────────────────────────────


def _require_state_key(
    state: TickState,
    key: str,
    *,
    node: str,
    hint: str,
) -> Any:
    """从 ``TickState`` 取 key，缺失立即 raise，附诊断信息。

    集中 raise 格式：未来需加 sentry tag / 节点名前缀 / 链路 id 都改这一处。
    """
    val = state.get(key)
    if val is None:
        raise ValueError(f"{node}: state[{key!r}] missing —— {hint}")
    return val


# ─────────────────────────────────────────────────────────────────────
# 节点真实现（顶层 _impl 可被独立测试 / IDE 跳转 / mypy 直接 narrow）
# ─────────────────────────────────────────────────────────────────────


def _write_state_impl(deps: PersistDeps, state: TickState) -> NodeReturn:
    """T3 第 1 节点真实现。

    14 §3.5：必跑节点；失败 → raise → daemon §5.5 处理。
    单事务原子写 ``tick`` + （sig>=7 时）``episode_recall``（已在
    ``insert_tick`` 内部完成）。rowid 写回 ``state["tick_id"]``。
    """
    node = "t3.persist.write_state"

    # 1. validate next_state（dict → State pydantic，过 8 层 + S-1 invariant）
    next_state_dict = _require_state_key(
        state,
        "next_state",
        node=node,
        hint="T1.sense.derive 应已 deepcopy(prev_state) 起 next_state",
    )
    next_state_validated = validate_state(next_state_dict)

    # 2. trigger_source / triggered_at 兜底
    trigger_source = _require_state_key(
        state,
        "trigger_source",
        node=node,
        hint="daemon invoke 必须注入",
    )
    triggered_at = _require_state_key(
        state,
        "triggered_at",
        node=node,
        hint="daemon invoke 必须注入",
    )

    # 3. act_decision / act_result（cold_start 允许 None；
    #    TickWriteParams._check_act_decision_required 会兜底校）
    act_decision = state.get("act_decision")
    act_result = state.get("act_result")  # may be missing key (T2 跳过)

    params = TickWriteParams(
        state=next_state_validated,
        trigger_source=trigger_source,
        triggered_at=triggered_at,
        note=state.get("note"),
        significance=state.get("significance"),
        act_decision=act_decision,
        act_result=act_result,
    )

    # 4. canonical tick 与可选 Relationship change 同事务原子写
    relationship_change = state.get("relationship_change")
    if relationship_change is not None and type(relationship_change) is not RelationshipChange:
        raise ValueError("t3.persist.write_state: relationship_change invalid")
    with deps.db.transaction():
        tick_id = deps.db.insert_tick(params)
        if relationship_change is not None:
            deps.db.apply_relationship_change(relationship_change, tick_id)
    _LOG.info(
        "T3.persist.write_state tick_id=%s trigger_source=%s significance=%s "
        "episode_candidate=%s relationship_change_present=%s",
        tick_id,
        trigger_source,
        params.significance,
        isinstance(params.significance, int) and params.significance >= 7,
        relationship_change is not None,
    )

    return {"tick_id": tick_id}


def _write_memory_impl(deps: PersistDeps, state: TickState) -> NodeReturn:
    """T3 第 2 节点真实现。

    14 §3.6：可容忍失败。三元组 (description, expire_at, tag) diff 找新增
    thoughts，单事务批量 INSERT。

    **失败语义**：节点 raise 时 thought 表少一笔事件流记录，但
    ``state.interior.thoughts`` 真相源已在 write_state 落 SQLite tick 表——
    心的"当下在想什么"不丢。

    **不存在自动重试**：下次 tick T1.sense_derive deepcopy(prev_state) 后
    next.thoughts == prev.thoughts，三元组判同 → 0 新增 → 0 INSERT。重试
    需要 LLM 重写该 thought（任一三元组字段变）才会作为新条 INSERT；
    或人为补 ``INSERT INTO thought ...``（运维场景）。thought 表与
    state 真相源不同步是 09 §4 设计意图，不是 bug。

    独立事务：与 write_state 事务分开，一个 thought 写失败不回滚整 tick。
    """
    node = "t3.persist.write_memory"

    tick_id = _require_state_key(
        state,
        "tick_id",
        node=node,
        hint="write_state 应已 patch（拓扑保证 T3 串行）",
    )

    prev_state_dict = state.get("prev_state") or {}
    next_state_dict = state.get("next_state") or {}
    prev_interior = cast("dict[str, object]", prev_state_dict.get("interior") or {})
    next_interior = cast("dict[str, object]", next_state_dict.get("interior") or {})
    prev_list_raw = cast("list[dict[str, object]]", prev_interior.get("thoughts") or [])
    next_list_raw = cast("list[dict[str, object]]", next_interior.get("thoughts") or [])

    prev_list = validate_thoughts(prev_list_raw)
    next_list = validate_thoughts(next_list_raw)

    # 三元组 diff：只 INSERT 新增，不 UPDATE 不 DELETE
    prev_keys = {(t.description, t.expire_at, t.tag) for t in prev_list}
    new_thoughts = [t for t in next_list if (t.description, t.expire_at, t.tag) not in prev_keys]

    if not new_thoughts:
        _LOG.info("T3.persist.write_memory tick_id=%s new_thoughts_count=0", tick_id)
        return {}

    # 14 §3.6：source_tick_id 用本 tick rowid，ts 用 next_state.time.iso
    next_time = cast("dict[str, object]", next_state_dict.get("time") or {})
    ts_iso = cast("str", next_time["iso"])

    with deps.db.transaction():
        deps.db.insert_thoughts(
            new_thoughts,
            ts=ts_iso,
            source_tick_id=tick_id,
        )
    _LOG.info(
        "T3.persist.write_memory tick_id=%s new_thoughts_count=%d",
        tick_id,
        len(new_thoughts),
    )

    return {}


# 三段 header（与 _bundle_layers 渲染输出一致；降级兜底也用同 header，09 §4.3）。
_HEADER_A = "## 最近 5 个 tick"
_HEADER_B = "## 今天"
_HEADER_C = "## 过去 7 天的高光（按近到远）"

# Layer B「今天」拉取上限：一天醒 5min/睡 1h 心跳，单日 tick 远不足此数（防异常爆量）。
_LAYER_B_DAY_POOL = 1000
# Layer C 高光回看窗口（天）。
_LAYER_C_LOOKBACK_DAYS = 7


def _seg_fallback(header: str, exc: object) -> str:
    """单段渲染失败兜底（09 §4.5：`> ⚠️ 本段渲染失败：<原因>`，不阻塞其他段）。"""
    return f"{header}\n\n> ⚠️ 本段渲染失败：{exc}"


def _safe_segment(header: str, label: str, render: Callable[[], str]) -> str:
    """跑一段渲染；该段失败只降级该段（N-2 / §4.5），不连累其他段。按 layer 打点。"""
    try:
        return render()
    except Exception as exc:  # noqa: BLE001 - 单段失败隔离，降级不崩 tick
        _LOG.warning("T3.persist.flush_bundle: %s 渲染失败，降级：%s", label, exc)
        return _seg_fallback(header, exc)


def _render_history_layers(deps: PersistDeps, now_iso: str) -> tuple[list[str], list[int]]:
    """渲染 Layer A/B/C，**逐段独立兜底**（§4.5）。返回 (三段文本, Layer C 选中的高光 tick_id)。

    时间基准用 **next_state.time.iso（她的世界时）**，不是墙钟：Layer B「今天」/ Layer C「7 天内」
    都相对她的时间线（ts 单时区 ISO，字符串比较即可）。选中的高光 id 交给调用方在 bundle 写成后
    衰减 cooldown（N-1 / §4.4.4.2）。
    """
    try:
        now_dt = datetime.fromisoformat(now_iso)
        day_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        lookback = (now_dt - timedelta(days=_LAYER_C_LOOKBACK_DAYS)).isoformat()
    except ValueError as exc:
        _LOG.warning("T3.persist.flush_bundle: now_iso 解析失败，三段降级：%s", exc)
        return (
            [_seg_fallback(h, exc) for h in (_HEADER_A, _HEADER_B, _HEADER_C)],
            [],
        )

    layer_a = _safe_segment(
        _HEADER_A,
        "Layer A",
        lambda: _layers.render_layer_a(
            deps.db.get_ticks_for_bundle(limit=_layers.LAYER_A_RAW_POOL)
        ),
    )
    layer_b = _safe_segment(
        _HEADER_B,
        "Layer B",
        lambda: _layers.render_layer_b(
            deps.db.get_ticks_for_bundle(limit=_LAYER_B_DAY_POOL, since_iso=day_start)
        ),
    )
    layer_c, recall_ids = _safe_layer_c(deps, lookback)
    return [layer_a, layer_b, layer_c], recall_ids


def _safe_layer_c(deps: PersistDeps, lookback: str) -> tuple[str, list[int]]:
    """Layer C 段：取高光（episode JOIN episode_recall, cooldown>0）+ 渲染 + 收集选中 id。

    选中 id 供调用方写 bundle 后衰减 cooldown（§4.4.4.2）。本段失败只降级本段、返空 id（N-2）。
    """
    try:
        rows = deps.db.get_highlight_episodes(since_iso=lookback, limit=_layers.LAYER_C_LIMIT)
        recall_ids = [r["id"] for r in rows if isinstance(r.get("id"), int)]
        return _layers.render_layer_c(rows), recall_ids
    except Exception as exc:  # noqa: BLE001 - 单段失败隔离
        _LOG.warning("T3.persist.flush_bundle: Layer C 渲染失败，降级：%s", exc)
        return _seg_fallback(_HEADER_C, exc), []


def _flush_bundle_impl(deps: PersistDeps, state: TickState) -> NodeReturn:
    """T3 第 3 节点真实现。

    14 §3.7：可容忍失败。

    NOW 段（09 §4.4.1）从 next_state 渲染；Layer A/B/C（§4.4.2/3/4）read-time 从库渲染，**逐段
    独立兜底**（§4.5：某段失败只降级该段、不连累其他段，node 容错语义不崩 tick）。Layer C 走
    ``episode JOIN episode_recall``（cooldown>0），bundle **写成后**对选中的高光 ``cooldown -= 50``
    （§4.4.4.2 F-1 自反馈防御，避免同一高光每 tick 重复闪回）——故 flush_bundle 对 DB 有这一处写。
    衰减容错（失败只记 warning，不崩 tick）。atomic tmpfile + rename 写入路径不变。
    """
    node = "t3.persist.flush_bundle"

    next_state_dict = _require_state_key(
        state,
        "next_state",
        node=node,
        hint="T1.sense.derive 应已 deepcopy(prev_state)",
    )
    next_state = validate_state(next_state_dict)
    now_iso = next_state.time.iso

    relationship_summary = (
        render_relationship_summary(require_user_relationship(deps.relationship_reader))
        if deps.relationship_reader is not None
        else ""
    )
    now_md = _render_now_section(
        next_state,
        state.get("note"),
        state.get("significance"),
        relationship_summary=relationship_summary,
    )
    layers, recall_ids = _render_history_layers(deps, now_iso)
    bundle_text = "\n\n".join([now_md, *layers])

    _atomic_write_text(deps.bundle_path, bundle_text)
    _LOG.info(
        "T3.persist.flush_bundle path=%s bundle_bytes=%d recalled=%d",
        deps.bundle_path,
        len(bundle_text.encode("utf-8")),
        len(recall_ids),
    )

    # N-1（§4.4.4.2）：bundle 成功写入后，衰减本轮闪回过的高光 cooldown。容错：失败只记
    # warning（高光下轮可能重复闪回），不崩 tick——与 flush_bundle 容错语义一致。
    if recall_ids:
        try:
            with deps.db.transaction():
                deps.db.decay_episode_recall(tick_ids=recall_ids, now_iso=now_iso)
        except Exception as exc:  # noqa: BLE001 - 衰减失败不崩 tick
            _LOG.warning(
                "T3.persist.flush_bundle: episode_recall 衰减失败（高光可能重复闪回）：%s", exc
            )

    return {}


# ─────────────────────────────────────────────────────────────────────
# Factory thin wrappers（绑 deps 返回 LangGraph 兼容签名 closure）
# ─────────────────────────────────────────────────────────────────────


def _make_write_state(deps: PersistDeps) -> Callable[[TickState], NodeReturn]:
    """绑 deps 返回 closure（LangGraph add_node 接口）。"""

    def write_state(state: TickState) -> NodeReturn:
        return _write_state_impl(deps, state)

    return write_state


def _make_write_memory(deps: PersistDeps) -> Callable[[TickState], NodeReturn]:
    """绑 deps 返回 closure（LangGraph add_node 接口）。"""

    def write_memory(state: TickState) -> NodeReturn:
        return _write_memory_impl(deps, state)

    return write_memory


def _make_flush_bundle(deps: PersistDeps) -> Callable[[TickState], NodeReturn]:
    """绑 deps 返回 closure（LangGraph add_node 接口）。"""

    def flush_bundle(state: TickState) -> NodeReturn:
        return _flush_bundle_impl(deps, state)

    return flush_bundle


# ─────────────────────────────────────────────────────────────────────
# 渲染辅助（minimal）
# ─────────────────────────────────────────────────────────────────────


def _render_now_section(
    state: State,
    note: str | None,
    significance: int | None,
    *,
    relationship_summary: str = "",
) -> str:
    """09 §4.4.1 NOW 段——固定字段 + 可选增强行。

    固定：时间 / 位置 / 天气 / 活动投影 / 物理 Presence / 身心胸三 gauge。
    可选（对应数据缺失则不渲染）：
    天象行（月相/日出日落/UV/降水）、心声（note）、重要度（significance）。
    渲染源：time / location / **environment（天气 + 天象）** / activity / interior。
    天气接进感知回路（此前「weather 待完整版补」TODO，2026-06-30 补全；详 09 §4.4.1）。
    """
    interior = state.interior
    activity_label = "刚结束" if state.activity.step == "settle" else "在做"
    others = "、".join(state.presence.others) or "无"
    lines = [
        "# 现在",
        f"- 时间：{state.time.iso}",
        f"- 位置：{state.location.name}",
        *_weather_lines(state.environment),
        f"- {activity_label}：{state.activity.name}",
        *current_possession_fact_lines(state.model_dump()),
        f"- 物理在场：user={'是' if state.presence.user_present else '否'}；其他={others}",
        (
            f"- 身/心/胸：body={interior.body.value} "
            f"mood={interior.mood.value} "
            f"inner_pulse={interior.inner_pulse.value}"
        ),
    ]
    if relationship_summary:
        lines.append(relationship_summary)
    if note:
        lines.append(f"- 心声：{note}")
    if significance is not None:
        lines.append(f"- 重要度：{significance}/10")
    return "\n".join(lines)


def _weather_lines(env: Environment) -> list[str]:
    """09 §4.4.1 NOW 段「天气」(+「天象」) 行。"""
    weather_bits = [env.weather, f"{env.temperature:g}°C"]
    if env.humidity:
        weather_bits.append(f"湿度{env.humidity}%")
    if env.wind:
        weather_bits.append(env.wind)
    lines = [f"- 天气：{' '.join(weather_bits)}"]

    sky_bits: list[str] = []
    if env.moon_phase:
        sky_bits.append(env.moon_phase)
    if env.sunrise and env.sunset:
        sky_bits.append(f"日出{env.sunrise} 日落{env.sunset}")
    if env.uv_index:
        sky_bits.append(f"UV{env.uv_index}")
    if env.precip_mm:
        sky_bits.append(f"降水{env.precip_mm:g}mm")
    if sky_bits:
        lines.append(f"- 天象：{' · '.join(sky_bits)}")
    return lines


def _atomic_write_text(path: Path, text: str) -> None:
    """tmpfile + rename atomic 写文本——避免 main agent 读到半文件（09 §4.2）。

    实现：``tempfile.NamedTemporaryFile`` 写到同目录（保证同文件系统，
    ``os.rename`` 原子性才成立），再 ``os.replace`` 替换。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # delete=False + 手动 rename：保证 atomic
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as f:
        f.write(text)
        tmp_path = Path(f.name)
    os.replace(tmp_path, path)


# ─────────────────────────────────────────────────────────────────────
# 顶层 mock 透传（保留兼容 build.py 默认装配）
# ─────────────────────────────────────────────────────────────────────
#
# build.py 默认走顶层 mock（拓扑测试不需要真 db）；真实接 db 时 build.py 接
# deps 参数走 closure。详 build.py docstring。
#
# 三个 mock 函数体相同（``del state; return {}``），合并成单实现 + 三个模块级
# 别名。spy 测试不受影响——``monkeypatch.setattr(nodes_mod, "t3_persist_write_state",
# spy)`` 替换的是模块属性，原 helper 对象不变；build.py:add_node attr lookup 拿到 spy。


def _t3_mock_pass_through(state: TickState) -> NodeReturn:
    """T3 mock 透传——三个节点共用占位（无 deps 注入时使用）。"""
    del state
    return {}


t3_persist_write_state = _t3_mock_pass_through
t3_persist_write_memory = _t3_mock_pass_through
t3_persist_flush_bundle = _t3_mock_pass_through


__all__ = [
    "PersistDeps",
    "make_persist_nodes",
    "t3_persist_flush_bundle",
    "t3_persist_write_memory",
    "t3_persist_write_state",
]
