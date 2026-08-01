"""运行时层 — 心的生命循环编排（平台无关）。

依赖：graph/, db/, llm/, adapters/（平台协议由 adapters/ 提供）

模块：
- daemon.py         常驻心跳进程（kindred run）；装配 adapters.openclaw.GatewayClient
- scheduler.py      tick 节奏控制（heartbeat 醒 5min / 睡 1h）+ 按 source 去抖
- watcher.py        MessageWatcher：轮询 main_session_messages → watcher
                    TriggerEvent（30min debounce；消息由 sense_io 按 cursor 消费）
- history_sync.py   拉 chat.history 进表（调 adapters.openclaw.gateway，「用 adapter 的桥」）
- io_bridge.py      心主动 push（调 adapters.openclaw.gateway.send_chat，同上）
- tick_graph / dream_graph / pidfile  纯生命逻辑

边界说明：gateway / device_identity 是 OpenClaw 协议层，已迁出到 adapters/openclaw/。
history_sync / io_bridge 是「用 adapter 的桥」（何时拉 / 写哪张表 /
何时 push 都是心的编排），留在 runtime。
"""
