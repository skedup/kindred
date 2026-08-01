"""嘴侧 bundle 的 Layer A/B/C 渲染（09-memory §4.4.2/4.4.3/4.4.4）。

``T3.persist.flush_bundle`` 每 tick 整文重写 ``context-bundle.md``：NOW 段在 persist.py
（当下切片），本模块补三段历史底色——**纯函数**，吃 tick dict（``db.ticks._row_to_dict`` 形态，
``interior`` / ``activity`` 已是解析后的 dict），出 Markdown：

- **Layer A 最近轨迹**：把最近 N 个 raw tick **read-time 压缩**成 ≤5 个 episode（相邻同 activity
  且 mood 变化 ≤ 阈值 → 合并一行），信息密度高、趋势可见（§4.4.2，继承 monica episodes.py）。
- **Layer B 当日摘要**：今天跑过的 activities + 平均 mood + 高光数（§4.4.3）。
- **Layer C 高光闪回**：最近 7 天 significance≥7 的高光，按 sig/ts top-N（§4.4.4）。取数走
  ``episode`` 视图 JOIN ``episode_recall``（``cooldown > 0``，F-1 自反馈防御），由
  ``db.get_highlight_episodes`` 完成；``flush_bundle`` 在 bundle 写成后对选中高光衰减 cooldown
  （``db.decay_episode_recall``，§4.4.4.2）——本模块只渲染传入的高光行，不碰 DB。

纯函数 + tick dict 入参——脱离 graph/db 可直接单测（取数/冷却衰减在 db + flush_bundle 侧）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# §4.4.2.1 配置（v0.1 默认取自 monica 实战 + 简化映射；Phase β 可提为 config）。
LAYER_A_LIMIT = 5  # episode 数上限
LAYER_A_RAW_POOL = 30  # 喂压缩函数的 raw tick 池
LAYER_A_MOOD_THRESHOLD = 8  # 相邻 tick mood.value 相似阈值
LAYER_B_MAX_ACTIVITIES = 8  # 当日摘要至多列几个 activity
LAYER_C_LIMIT = 5  # 高光闪回条数


def _mood(tick: dict[str, Any]) -> int | None:
    """从 tick dict 取 interior.mood.value（缺失/脏数据 → None，渲染降级）。"""
    interior = tick.get("interior")
    if not isinstance(interior, dict):
        return None
    mood = interior.get("mood")
    if not isinstance(mood, dict):
        return None
    value = mood.get("value")
    return value if isinstance(value, int) else None


def _activity_name(tick: dict[str, Any]) -> str:
    activity = tick.get("activity")
    if isinstance(activity, dict):
        name = activity.get("name")
        if isinstance(name, str) and name:
            return name
    return "?"


def _layer_a_activity_label(tick: dict[str, Any]) -> str:
    """Layer A 单独标出谢幕态，不影响 Layer B/C 的 activity 统计语义。"""
    name = _activity_name(tick)
    activity = tick.get("activity")
    if isinstance(activity, dict) and activity.get("step") == "settle":
        return f"{name} / settle"
    return name


def _sig(tick: dict[str, Any]) -> int:
    value = tick.get("significance")
    return value if isinstance(value, int) else 0


def _hhmm(ts: Any) -> str:
    """从 ISO 字符串取 HH:MM（解析失败回退原值，不抛）。"""
    if not isinstance(ts, str):
        return "?"
    # ISO: 2026-06-24T17:25:00+08:00 → 取 T 后 5 位
    if "T" in ts:
        tail = ts.split("T", 1)[1]
        return tail[:5] if len(tail) >= 5 else tail
    return ts


def _first_line(note: Any, *, limit: int = 40) -> str:
    if not isinstance(note, str) or not note.strip():
        return ""
    head = note.strip().splitlines()[0]
    return head if len(head) <= limit else head[:limit] + "…"


def _minutes_between(start_iso: Any, end_iso: Any) -> int | None:
    """两 ISO 时刻相差分钟（>=0，四舍五入）；解析失败 → None。单时区前提（her life）。"""
    if not isinstance(start_iso, str) or not isinstance(end_iso, str):
        return None
    try:
        delta = datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)
    except ValueError:
        return None
    return max(0, round(delta.total_seconds() / 60))


def _started_at(tick: dict[str, Any]) -> str | None:
    activity = tick.get("activity")
    if isinstance(activity, dict):
        started = activity.get("started_at")
        if isinstance(started, str) and started:
            return started
    return None


# ─── Layer A：最近轨迹（压缩）────────────────────────────────────────


def compress_layer_a(
    ticks_desc: list[dict[str, Any]],
    *,
    max_episodes: int = LAYER_A_LIMIT,
    mood_threshold: int = LAYER_A_MOOD_THRESHOLD,
) -> list[dict[str, Any]]:
    """把 raw tick（newest-first）压缩成 episode（newest-first，最多 max_episodes 个）。

    合并规则（§4.4.2 / K-34）：相邻两 tick **Layer A 活动标签相同** 且
    **mood.value 变化 ≤ 阈值** → 并入同 episode。``settle`` 使用 ``<name> / settle`` 标签，
    因而会与同名活动的执行段拆开；Layer B/C 仍使用原始 activity.name。活动标签变化 / mood 跳变
    > 阈值 → 起新 episode。mood 缺失则不以 mood 拆。每个 episode：start/end_ts、tick_count、
    activity、mood 起→止、sig 取最大、note 取该段最新。
    """
    if not ticks_desc:
        return []
    asc = list(reversed(ticks_desc))  # 时间正序合并

    episodes: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    prev_mood: int | None = None

    for tick in asc:
        name = _layer_a_activity_label(tick)
        mood = _mood(tick)
        mergeable = (
            cur is not None
            and cur["activity"] == name
            and (prev_mood is None or mood is None or abs(mood - prev_mood) <= mood_threshold)
        )
        if mergeable and cur is not None:
            cur["end_ts"] = tick.get("ts")
            cur["tick_count"] += 1
            cur["sig"] = max(cur["sig"], _sig(tick))
            if mood is not None:
                cur["mood_end"] = mood
            cur["last_note"] = tick.get("note") or cur["last_note"]
        else:
            cur = {
                "activity": name,
                "start_ts": tick.get("ts"),
                "end_ts": tick.get("ts"),
                "tick_count": 1,
                "sig": _sig(tick),
                "mood_start": mood,
                "mood_end": mood,
                "last_note": tick.get("note"),
            }
            episodes.append(cur)
        prev_mood = mood

    # 取最近 max_episodes 个，渲染时 newest-first。
    recent = episodes[-max_episodes:]
    return list(reversed(recent))


def render_layer_a(ticks_desc: list[dict[str, Any]]) -> str:
    """渲染 Layer A（newest-first 压缩 episode 行，§4.3/§4.4.2）：

    ``[起 → 止] (持续时长, Ntick) activity · sig=最大 · mood:起→止 ⬆/⬇/—`` + ``  → note``。
    单 tick / 0 跨度省略时长（只 ``(Ntick)``）。空 → 占位。
    """
    episodes = compress_layer_a(ticks_desc)
    if not episodes:
        return "## 最近 5 个 tick\n\n> 暂无轨迹"
    lines = ["## 最近 5 个 tick"]
    for ep in episodes:
        start, end = _hhmm(ep["start_ts"]), _hhmm(ep["end_ts"])
        span = start if start == end else f"{start} → {end}"
        span_min = _minutes_between(ep["start_ts"], ep["end_ts"])
        if ep["tick_count"] == 1 or not span_min:
            label = f"{ep['tick_count']}tick"
        else:
            label = f"{span_min}分钟, {ep['tick_count']}tick"
        mood_str = _render_mood_trend(ep["mood_start"], ep["mood_end"])
        lines.append(f"[{span}] ({label}) {ep['activity']} · sig={ep['sig']}{mood_str}")
        note = _first_line(ep["last_note"])
        if note:
            lines.append(f"  → {note}")
    return "\n".join(lines)


def _render_mood_trend(start: int | None, end: int | None) -> str:
    if start is None or end is None:
        return ""
    arrow = "⬆" if end > start else "⬇" if end < start else "—"
    return f" · mood:{start}→{end} {arrow}"


# ─── Layer B：当日摘要 ───────────────────────────────────────────────


def _activity_durations(ticks: list[dict[str, Any]]) -> dict[str, int]:
    """每个 activity 当日累计时长（分钟，§4.4.3「累计 now - started_at」）。

    started_at 在一段活动内跨 tick 不变（``act_llm`` advance 不覆盖），故按
    ``(name, started_at)`` 分段，每段时长 = 段内 max(ts) - started_at，再按 name 累加。
    """
    runs: dict[tuple[str, str], int] = {}
    for tick in ticks:
        name = _activity_name(tick)
        started = _started_at(tick)
        if started is None:
            continue
        mins = _minutes_between(started, tick.get("ts"))
        if mins is None:
            continue
        key = (name, started)
        runs[key] = max(runs.get(key, 0), mins)
    totals: dict[str, int] = {}
    for (name, _started), mins in runs.items():
        totals[name] = totals.get(name, 0) + mins
    return totals


def render_layer_b(today_ticks: list[dict[str, Any]]) -> str:
    """今天跑过的 activities（带累计时长，按时长降序至多 N）+ 平均 mood + 高光数（§4.3/§4.4.3）。

    ``## 今天`` / ``- 跑过的 activities：a(Nmin) / b(Nmin) / …`` / ``- 平均 mood：N`` /
    ``- 高光时刻数：N``。时长不可得（无 started_at）的 activity 退回仅列名。
    """
    if not today_ticks:
        return "## 今天\n\n> 今天还没有 tick"

    durations = _activity_durations(today_ticks)
    moods: list[int] = []
    highlights = 0
    seen: list[str] = []  # 保活动出现序，给无时长的兜底
    for tick in today_ticks:
        name = _activity_name(tick)
        if name not in seen:
            seen.append(name)
        mood = _mood(tick)
        if mood is not None:
            moods.append(mood)
        if _sig(tick) >= 7:
            highlights += 1

    if durations:
        ranked = sorted(durations.items(), key=lambda kv: kv[1], reverse=True)
        shown = ranked[:LAYER_B_MAX_ACTIVITIES]
        act_str = " / ".join(f"{name}({mins}min)" for name, mins in shown)
        extra = len(ranked) - LAYER_B_MAX_ACTIVITIES
    else:  # 无 started_at（时长不可得）→ 退回仅列活动名
        shown_names = seen[:LAYER_B_MAX_ACTIVITIES]
        act_str = " / ".join(shown_names)
        extra = len(seen) - LAYER_B_MAX_ACTIVITIES
    if extra > 0:
        act_str += f" / 其他 {extra} 项"
    avg_mood = round(sum(moods) / len(moods)) if moods else "—"

    return "\n".join(
        [
            "## 今天",
            f"- 跑过的 activities：{act_str}",
            f"- 平均 mood：{avg_mood}",
            f"- 高光时刻数：{highlights}",
        ]
    )


# ─── Layer C：高光闪回 ───────────────────────────────────────────────


def render_layer_c(highlight_ticks: list[dict[str, Any]]) -> str:
    """渲染最近 7 天 significance≥7 高光（调用方已按 sig/ts 排序截断，§4.3/§4.4.4）：

    ``[YYYY-MM-DD HH:MM] sig=N · <note 首句>``（note 缺失退回 activity 名）。空 → 占位。
    """
    if not highlight_ticks:
        return "## 过去 7 天的高光（按近到远）\n\n> 暂无高光"
    lines = ["## 过去 7 天的高光（按近到远）"]
    for tick in highlight_ticks:
        ts = tick.get("ts")
        date = ts.split("T", 1)[0] if isinstance(ts, str) and "T" in ts else ts
        content = _first_line(tick.get("note"), limit=50) or _activity_name(tick)
        lines.append(f"[{date} {_hhmm(ts)}] sig={_sig(tick)} · {content}")
    return "\n".join(lines)


__all__ = [
    "LAYER_A_LIMIT",
    "LAYER_A_RAW_POOL",
    "LAYER_C_LIMIT",
    "compress_layer_a",
    "render_layer_a",
    "render_layer_b",
    "render_layer_c",
]
