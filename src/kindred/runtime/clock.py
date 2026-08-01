"""Life-clock helpers.

Kindred 的 tick 时间属于 ta 所在的世界，不属于宿主进程。这里用配置里的
IANA timezone 显式生成 aware datetime / ISO 字符串，避免 openclaw / launchd
等运行环境默认 UTC 时把 ``triggered_at`` 和 ``time.phase`` 算偏。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def life_now(timezone_name: str) -> datetime:
    """Return current wall-clock time in the configured life timezone."""
    return datetime.now(tz=ZoneInfo(timezone_name))


def life_now_iso(timezone_name: str, *, timespec: str = "seconds") -> str:
    """Return current life time as ISO8601 with timezone offset."""
    return life_now(timezone_name).isoformat(timespec=timespec)


__all__ = ["life_now", "life_now_iso"]
