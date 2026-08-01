"""IOBridge —— 心（子 agent / graph 节点）的出向桥。

graph 内部节点要**主动联系 user** 时走本桥，不直接碰 ws / OpenClaw API
（docs/13 §302）。本桥只补「心主动 push」一条出向链路：

    act 节点（LLM 调 send_to_user 工具）
        → IOBridge.send_to_user(text)      # 经 ws 把话发给嘴的 session

═══ 出向通道：复用 openclaw 子 agent 的 announce 传输（2026-06-29 改） ═══

历史（earlier milestone）：心 push 曾以 **user 角色** ``chat.send`` 注入嘴 session（带
``[心声·请原话转达]`` 前缀），由嘴「原话转达」。问题：openclaw 把心说的话当成
**用户发的** → 表里记成 partner、web 渲染成 user 气泡，还得靠前缀 + echo 过滤
打补丁（Bug2）。

现在（哲学 B：心定意、嘴用自己声音重表达）：``send_to_user`` 改走 openclaw 子
agent **announce 的同一传输** —— ``chat.send`` + ``systemInputProvenance
{kind:"inter_session", sourceTool:"kindred_heart"}`` + ``suppressCommandInterpretation``，
**不加任何前缀**。嘴收到后 openclaw 会把它框成「别处路由来的、不是终端用户的直接
指令」，嘴跑一轮**用自己声音重表达**并经正常 delivery 投到 channel。

设计留档：``docs/discussions/2026-06-29-bug2-heart-push-via-inter-session.md``。

═══ 为什么不再写表（record_outbound 退役） ═══

旧 ``record_outbound`` 写一条 ``my_heart`` 行，是为了让心/嘴看得到「心刚说过啥」。
新链路里这件事由 openclaw 原生承担：嘴重表达的 **assistant 回复**进 transcript →
``HistorySync`` 拉回成 ``my_voice`` = 唯一真相源（嘴下轮读到、心下个 tick 也读到、
也正是 channel 上收到的那句）。心自己那条 inter-session 触发消息回流时由
``messages.build_from_gateway`` 按 ``provenance`` 跳过（不入表、watcher 不自唤醒），
取代旧的 text+时间窗 echo 过滤。故出向桥不再需要 db。

═══ 复用 gateway.py 的 ws 底座 ═══

``GatewayClient`` 已封装 connect/auth + ``send_chat``（支持 provenance /
suppressCommandInterpretation）。本桥持有一个 ``GatewayClient``，调 ``send_chat`` 即可。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kindred.db.messages import (
    HEART_INTER_SESSION_SOURCE_TOOL,
    PROVENANCE_KIND_INTER_SESSION,
)

if TYPE_CHECKING:
    from kindred.adapters.openclaw.gateway import GatewayClient
    from kindred.openclaw import OpenClawWire

logger = logging.getLogger(__name__)

# inter-session provenance 的来源标识（心侧）。``sourceSessionKey`` / ``sourceChannel``
# 是给嘴看到的 prompt 框注的信息（非路由依据）；``sourceTool`` 是过滤回流的关键键
# （与 ``messages.HEART_INTER_SESSION_SOURCE_TOOL`` 一致，build_from_gateway 据此跳过）。
_HEART_SOURCE_SESSION_KEY = "kindred-heart"
_HEART_SOURCE_CHANNEL = "internal"


class IOBridgeError(Exception):
    """IOBridge 出向失败（ws 发送失败）。

    调用方（act 节点胶水）catch 后应**降级不崩 tick**——push 失败是运行时问题，
    不该让整个 graph tick 炸（与 act 节点「failure 不 raise」哲学一致）。
    """

    def __init__(self, message: str, *, unknown_side_effect: bool = False) -> None:
        super().__init__(message)
        self.unknown_side_effect = unknown_side_effect


class IOBridge:
    """心的出向桥：send_to_user（经 inter-session announce 传输）。

    持有一个 ``GatewayClient``（出向 ws），由调用方注入——便于测试用 fake gateway。

    Parameters
    ----------
    gateway
        已配置的 ``GatewayClient``（host/port/token/identity 就绪）。出向走它的
        ``send_chat``。测试可注入任何提供
        ``send_chat(session_key, text, *, provenance=..., suppress_command_interpretation=...)
        -> dict`` 的 fake。
    wire
        经同一条 direct session 验证的 transcript、approved peer 与 outbound route。
        任一部分缺失时 composition root 不构造本桥。
    """

    def __init__(
        self,
        gateway: GatewayClient,
        *,
        wire: OpenClawWire,
    ) -> None:
        self._gateway = gateway
        self._wire = wire

    # ── 出向：心主动联系 user ──────────────────────────────────

    def send_to_user(self, text: str) -> None:
        """把心此刻想说的话经 inter-session 传输交给嘴，由嘴重表达并投递。

        这是心主动 push 的核心出向动作：act 工具环里的 ``send_to_user`` handler
        取模型传入文本调本方法；真实工具调用与结果随后进入 ``act_result.tool_trace``。

        机制：``GatewayClient.send_chat`` 以 ``inter_session`` provenance（sourceTool
        ``kindred_heart``）+ ``suppressCommandInterpretation`` 注入嘴 session 触发一轮
        （异步 ack）。嘴被框成「这是别处路由来的、非终端用户的指令」→ 用自己声音
        重表达。决策在心、表达经嘴（见模块 docstring）。

        **投递路由**：只使用与 approved peer 同源对账过的 ``outbound_route``。
        本桥始终传 ``deliver=True`` 和完整 originating 路由；route 缺失或漂移时，
        composition root 不构造本桥，不回退到最近 session 或 web-only 发送。

        失败处理：发送失败 raise :class:`IOBridgeError`，由 act 节点胶水 catch 降级
        （push 失败不崩 tick）。无写表步骤，故无半成态。

        Parameters
        ----------
        text
            要发给 user 的话（非空）。
        """
        clean = (text or "").strip()
        if not clean:
            raise IOBridgeError("send_to_user: text 为空，拒绝发空消息")

        provenance = {
            "kind": PROVENANCE_KIND_INTER_SESSION,
            "sourceSessionKey": _HEART_SOURCE_SESSION_KEY,
            "sourceChannel": _HEART_SOURCE_CHANNEL,
            "sourceTool": HEART_INTER_SESSION_SOURCE_TOOL,
        }
        route = self._wire.outbound_route
        resp = self._gateway.send_chat(
            self._wire.transcript_session,
            clean,
            provenance=provenance,
            suppress_command_interpretation=True,
            deliver=True,
            originating_channel=route.channel,
            originating_to=route.target,
            originating_account_id=route.account_id,
            originating_thread_id=route.thread_id,
        )
        payload = resp.get("payload") if isinstance(resp, dict) else None
        if (
            not isinstance(resp, dict)
            or resp.get("ok") is not True
            or not isinstance(payload, dict)
            or payload.get("status") != "started"
        ):
            unknown = isinstance(resp, dict) and resp.get("side_effect") == "unknown"
            raise IOBridgeError(
                "send_to_user: Gateway dispatch failed",
                unknown_side_effect=unknown,
            )

        logger.info(
            "IOBridge.send_to_user: dispatch accepted (inter-session announce, len=%d)",
            len(clean),
        )


__all__ = ["IOBridge", "IOBridgeError"]
