"""做梦时点调度 —— 「幂等补偿」语义的 ``should_dream``（D6.8）。

参考文档：docs/11-dreaming.md §9.1（默认 04:00）、docs/14 §2.1（daemon 主循环
``due = should_dream(now, index_path); if due is not None: dream_graph.invoke(...); continue``）。

为什么不是「窗口命中」
======================

做梦时点固定 04:00，但 daemon 睡眠态轮询间隔 1h（``SLEEP_INTERVAL_SECONDS``）。
03:30 醒、04:30 醒——04:00 那个时刻整个被跨过，「现在是不是正好 04:00±5min」永远
撞不上。加上 daemon 会卡（曾卡 4322min）、会重启，**窗口命中语义在会睡 / 会卡 /
会重启的进程上根本不成立**。

所以换成**幂等补偿**：

    should_dream(now, index_path) =
        「now 已过的最近一个 04:00 触发点」对应的 dream_date 还没被总结过
        （读 index_path 最近 morning_dream）→ 返回该 dream_date（该补做）；否则 None

- 04:00 还醒着 → 当场做
- 睡到 04:30 才轮询醒 → 补做（晚半小时，user 还在睡）
- daemon 卡到 06:00 才重启 → 一启动发现今天该做没做，补做
- **错过不丢、重启不漏、永不重复**（已做过的 dream_date 不再做）

只补最近一个
============

卡了 3 天错过 3 个 dream_date 时**不回填历史**——只补最近该做的那个，避免一次启动
连跑 3 次 LLM。对齐文档「第二天清晨重试」语义（11 §8 失败表）。

真相源
======

「某 dream_date 做过没」不新建状态——读 ``soul-history/index.json``（D6.6.3a 建）。
index entry 的 ``timestamp`` = ``{dream_date}T04:00:00+08:00``，**日期部分就是
dream_date**（被总结的昨日）。取最近一条 ``trigger=morning_dream`` 的 dream_date 与
「该补做的 dream_date」比即可（``YYYY-MM-DD`` 字典序 == 日期序）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING

from kindred.graph.dream._land_journal import _DREAM_HOUR, _DREAM_TZ_OFFSET

if TYPE_CHECKING:
    from pathlib import Path

_LOG = logging.getLogger(__name__)

# 触发点小时（与 _land_journal 的 dream_date+04:00 同源；从 "04:00:00" 取 4）
_DREAM_HOUR_INT = int(_DREAM_HOUR.split(":", 1)[0])


def _due_dream_date(now: datetime) -> str:
    """now（aware）对应「最近已过的 04:00 触发点」要总结的 dream_date（YYYY-MM-DD）。

    触发点 = 每天本地 04:00。``dream_date`` = 该触发点要总结的「昨日」= 触发日 − 1。

    推导（now 先归一到 +08:00 本地时区，再减 4h 把「04:00 之前」算进前一触发日）::

        anchor = now_local − 4h
        触发日 = anchor.date()
        dream_date = 触发日 − 1 天

    例：now=今天04:30 → anchor=今天00:30 → 触发日=今天 → dream_date=昨天 ✓
        now=今天03:00 → anchor=昨天23:00 → 触发日=昨天 → dream_date=前天 ✓
        （还没到今天 04:00，最近已过的触发点是昨天 04:00，总结前天）
    """
    tz = _dream_tz()
    aware = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    local = aware.astimezone(tz)
    anchor = local - timedelta(hours=_DREAM_HOUR_INT)
    trigger_day = anchor.date()
    return (trigger_day - timedelta(days=1)).strftime("%Y-%m-%d")


def _dream_tz() -> tzinfo:
    """从 ``_DREAM_TZ_OFFSET``（如 "+08:00"）构造 tzinfo（与 _land_journal 同源）。"""
    sign = 1 if _DREAM_TZ_OFFSET[0] == "+" else -1
    hh, mm = _DREAM_TZ_OFFSET[1:].split(":")
    return timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))


def _latest_dreamed_date(index_path: Path) -> str | None:
    """读 index.json，返回最近一条 ``trigger=morning_dream`` 的 dream_date（YYYY-MM-DD）。

    fail-safe：不存在 / 损坏 / 无合法 morning_dream 条目 → None（当作「从没做过」，
    交由 should_dream 决定补做——坏 index 不应阻断做梦）。timestamp 形如
    ``2026-06-11T04:00:00+08:00``，取 'T' 前的日期段。

    N-1：日期段必须严格校验 YYYY-MM-DD。否则一条合法 JSON 里的坏 timestamp
    （如 ``not-a-dateT04:00:00+08:00``）进 max() 会得 ``not-a-date``，字典序
    ``not-a-date > 2026-06-11`` → should_dream 返 None → 坏 entry 反而永久阻断做梦
    （与 fail-safe 目标相反）。非法 entry 忽略 + warning；全部非法 → None 交由补做。
    """
    if not index_path.exists():
        return None
    try:
        loaded = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        _LOG.warning("should_dream: index.json 损坏 / 不可读，当从未做梦 %s", index_path)
        return None
    snapshots = loaded.get("snapshots") if isinstance(loaded, dict) else None
    if not isinstance(snapshots, list):
        return None
    dates: list[str] = []
    for s in snapshots:
        if not _is_morning_dream(s):
            continue
        ts = s.get("timestamp")
        if not isinstance(ts, str) or "T" not in ts:
            continue
        date_part = ts.split("T", 1)[0]
        if _is_valid_ymd(date_part):
            dates.append(date_part)
        else:
            _LOG.warning("should_dream: 忽略非法 morning_dream timestamp %r", ts)
    return max(dates) if dates else None


def _is_valid_ymd(value: str) -> bool:
    """严格 YYYY-MM-DD（零填充）校验：round-trip——strptime 接受 2026-6-1，靠
    strftime 回原值反验卡死规范形（与 CLI --dream-date 同策略）。"""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")  # noqa: DTZ007 - 仅校验格式
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%d") == value


def _is_morning_dream(entry: object) -> bool:
    from collections.abc import Mapping

    return isinstance(entry, Mapping) and entry.get("trigger") == "morning_dream"


def should_dream(now: datetime, index_path: Path) -> str | None:
    """幂等补偿判断：该不该现在做梦？返回该补做的 dream_date，或 None（不做）。

    - 算出 now 对应「最近已过 04:00 触发点」要总结的 ``due`` dream_date
    - 读 index 最近做过的 dream_date ``latest``
    - ``latest is None`` 或 ``latest < due`` → 返回 ``due``（该补做；只补最近一个）
    - ``latest >= due`` → None（今天该做的已做过 / 或还在更新的将来，不重复）

    字符串比较即日期比较（``YYYY-MM-DD`` 字典序 == 时间序）。
    """
    due = _due_dream_date(now)
    latest = _latest_dreamed_date(index_path)
    if latest is None or latest < due:
        return due
    return None
