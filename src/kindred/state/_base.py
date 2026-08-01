"""Kindred state model 共同基类。

所有 state 层 pydantic model 继承 StrictBase，统一以下行为：
- ``extra="forbid"``：未知字段直接 ValidationError，避免 LLM 输出污染 / 字段名漂移

未来如需引入 `validate_assignment` / `frozen` / `str_strip_whitespace` 等全局选项，
统一在此处加一行即可，无需修改 15 处 model 定义。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictBase(BaseModel):
    """所有 state model 的共同基类。"""

    model_config = ConfigDict(extra="forbid")
