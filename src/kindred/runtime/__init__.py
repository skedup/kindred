"""运行时层 — 心的生命循环编排（平台无关）。

依赖：graph/, db/, llm/, mouth_host/（平台协议由宿主 wrapper 提供）

模块：
- daemon.py         常驻心跳进程（kindred run）；在 composition root 装配宿主 wrapper
- scheduler.py      tick 节奏控制（heartbeat 醒 5min / 睡 1h）+ 按 source 去抖
- watcher.py        MessageWatcher：轮询 main_session_messages → watcher
                    TriggerEvent（30min debounce；消息由 sense_io 按 cursor 消费）
- history_sync.py   从中立 TranscriptSource 拉取并写入 messages
- io_bridge.py      通过中立 OutboundChannel 执行心主动 push
- tick_graph / dream_graph / pidfile  纯生命逻辑

边界说明：gateway / device_identity 和 raw transcript/route 解释留在 OpenClaw；
history_sync / io_bridge 只负责平台无关的本地事务与发送编排。
"""
