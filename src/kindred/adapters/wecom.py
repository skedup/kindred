"""企业微信适配占位 —— 当前无独立实现。

现状：心给 user 发消息**统一走 OpenClaw 通道**（``adapters.openclaw.gateway``
的 ``send_chat``），而 OpenClaw 本身已接企业微信，所以无需独立的企微适配。

何时才需要本模块：若将来要**绕过 OpenClaw 直连企微 API**（如独立部署、
不依赖 OpenClaw 运行时），再照 ``adapters/openclaw/`` 的形状实现。

在那之前这里保持空占位，不写假接口。
"""
