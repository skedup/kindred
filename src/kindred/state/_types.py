"""Kindred state 层共用类型别名。

替代散落在 19 处的 ``Field(min_length=1)``：
- 含义模糊（"非空" 是字符长度还是去空白后非空？）
- 对 ISO8601 时间字段没有真正的格式校验

这里集中定义两个 Annotated alias，state model 引用即可：

- ``IsoDatetime``：必须是 timezone-aware ISO8601 字符串
  （``datetime.fromisoformat`` 解析后 ``tzinfo`` 不为 None）。
  拒 ``'x'`` / ``''`` / 纯日期 ``'2026-06-02'`` / 不带时区 ``'2026-06-02T18:59:00'``。
- ``NonBlankStr``：``strip()`` 后非空字符串。拒 ``''`` / ``'  '`` / ``'\\n'``。

为什么不直接用 ``datetime``：
- ISO8601 字符串 round-trip 通过 SQLite JSON 列时，原样保存 / 读出更稳定
  （datetime 反序列化要看驱动）
- LLM 生成 / human 写文档时直接读 ISO 字符串比反序列化的 datetime repr 更直观
- 校验在 ingress 已完成（一次解析），下游使用时不需要再次校验
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator

# 单段小写安全名（activity 名 / location binding id / place slot id 共用）：
# 拒空、大写、数字开头、``/`` 与 ``..``（防 path traversal / key 漂移）。
# 真相源在此（state 是最底层，activity/graph 都可 import；反向会循环依赖）。
SAFE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def is_safe_name(value: str) -> bool:
    """``value`` 是否是合法的单段小写安全名（``^[a-z][a-z0-9_]*$``）。"""
    return bool(SAFE_NAME_PATTERN.match(value))


def _validate_iso_datetime(value: str) -> str:
    """要求 timezone-aware ISO8601 字符串。"""
    if not isinstance(value, str):
        raise TypeError(f"expected str, got {type(value).__name__}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid ISO8601 datetime: {value!r} ({exc})") from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"ISO8601 datetime must be timezone-aware (got {value!r}); "
            "include offset like '+08:00' or 'Z'"
        )
    # 拒纯日期：fromisoformat('2026-06-02') 也能过，但语义是 datetime
    # 检查原字符串是否含 T / 空格分隔的时间部分
    if "T" not in value and " " not in value:
        raise ValueError(f"ISO8601 datetime must include time component (got date-only {value!r})")
    return value


def _validate_nonblank(value: str) -> str:
    """要求 strip() 后非空。"""
    if not isinstance(value, str):
        raise TypeError(f"expected str, got {type(value).__name__}")
    if not value.strip():
        raise ValueError("string must not be blank (empty or whitespace-only)")
    return value


IsoDatetime = Annotated[str, AfterValidator(_validate_iso_datetime)]
"""必须是 timezone-aware ISO8601 datetime 字符串。

合法：``"2026-06-02T18:59:00+08:00"`` / ``"2026-06-02T18:59:00Z"``
非法：``"2026-06-02"`` / ``"2026-06-02T18:59:00"`` / ``""`` / ``"x"``
"""


NonBlankStr = Annotated[str, AfterValidator(_validate_nonblank)]
"""``strip()`` 后非空的字符串。

合法：``"hello"`` / ``"  hello  "``
非法：``""`` / ``"   "`` / ``"\\n"``
"""
