"""dream 昨日窗口时间算法 —— summarize / reset_stack 共享。

参考文档：docs/11-dreaming.md §4.1。

抽出 ``day_window_ms`` 单点：Step 1.summarize 读昨日窗口、Step 2.reset_stack
bounded prune 昨日窗口，两者**必须用同一个区间** ``[since_ms, until_ms)``
（``= [dream_date 00:00, dream_date+1 00:00)``），否则会漂移——summarize 总结到某点、
reset 删到另一点，导致漏总结、误删今天、或误删 since 之前未总结 backlog。
单一真相源消除这类同型坑。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def day_window_ms(
    dream_date: str,
    triggered_at: Any,
) -> tuple[int | None, int | None]:
    """算 ``[dream_date 00:00, dream_date+1 00:00)`` 的 epoch ms 半开窗口。

    时区取 ``triggered_at`` 的 tzinfo（life 时钟一致）；triggered_at 缺失/非法时
    退化为 dream_date naive 解析（仍尽量产出窗口，避免直接放弃）。

    返回 ``(since_ms, until_ms)``；dream_date 无法解析时返 ``(None, None)``。

    - summarize 用 ``[since_ms, until_ms)`` 读昨日整天消息
    - reset_stack 也用同一 ``[since_ms, until_ms)`` 做 bounded prune（只删本次
      summary 覆盖范围，不动 ``since`` 之前可能未总结的 backlog，也不动 ``until``
      之后今日凌晨的「今天」消息，两头都留待重试/下次做梦）。
    """
    try:
        day = datetime.strptime(dream_date, "%Y-%m-%d")  # noqa: DTZ007 - tz 下面补
    except (ValueError, TypeError):
        return None, None

    tzinfo = None
    if isinstance(triggered_at, str):
        try:
            tzinfo = datetime.fromisoformat(triggered_at).tzinfo
        except ValueError:
            tzinfo = None

    start = day.replace(tzinfo=tzinfo) if tzinfo is not None else day
    end = start + timedelta(days=1)
    # naive（无 tzinfo）时 timestamp() 按本地时区解释——退化路径，可接受。
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)
