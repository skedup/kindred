"""Kindred — 一个能呼吸的 AI 伴侣框架。

呼吸 = SQLite 里真的有一行 tick 数据。

依赖纪律（6 层）：

    cli ─────────────┐
                     ▼
    runtime ──── adapters
       │             │
       ▼             ▼
    graph ──────────────────► llm
       │
       ▼
    state ◄───── db

铁律：
- state/ 不依赖任何东西（纯数据类型）
- db/ 只依赖 state/
- graph/ 不直接调 db/，通过参数注入
- runtime/ 是唯一组装一切的地方
- adapters/ 双向：runtime 调它发消息，它也接收外部 webhook
"""

__version__ = "0.3.0"
