"""外部世界适配层 — 跟具体平台协议对接的边界。

判定一个模块是不是 adapter：「换一个聊天平台，这个模块要重写吗？」
要 → 是 adapter（平台相关）；不用 → 是 runtime（心的生命逻辑，平台无关）。

子包 / 模块：
- openclaw/         OpenClaw 平台适配（gateway WS JSON-RPC + v4 device pairing）✅已实现

未来可扩展：telegram/ · discord/ · signal/ …（照 openclaw/gateway.py 的形状新建）

注：**bundle 注入不在本层**——它是 OpenClaw 运行时的 agent:bootstrap JS hook
（hooks/kindred-voice-bundle/），Python 包碰不到 system prompt。别再写进 adapters 职责。
"""
