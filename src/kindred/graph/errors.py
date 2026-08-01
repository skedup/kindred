"""graph 层对外公共异常契约（public facade）。

``NodeContractError`` 不是 graph 内部 helper——它是 **entry/runtime 跨层契约**：
CLI 单跑 graph、daemon 跑 tick_graph 时用它兜住节点契约违例（缺 state key /
非法字段等）。因此它必须从一个**不带下划线的稳定公共边界**导出，而不是让
``kindred.graph._shared._errors`` 这个 graph 内部底座变成运行时 API。

边界约定（earlier milestone codex review N-2）：
- **entry/runtime**（``cli`` / ``runtime.daemon`` 等 graph 之外的调用方）
  → 从本模块 ``kindred.graph.errors`` import。
- **graph 内部节点**（``graph.tick`` / 未来 ``graph.dream``）
  → 继续从 ``graph._shared._errors`` import（内部复用，可随 graph 重构自由调整）。

这样 ``_shared`` 为 tick/dream 内部复用做调整时，不会波及 entry/runtime。
"""

from __future__ import annotations

from kindred.graph._shared._errors import NodeContractError

__all__ = ["NodeContractError"]
