"""LLM 客户端契约（Protocol）。

参考文档：docs/14-heart-graph.md §3.3 (sense.llm) / §3.4 (act.llm)

为什么要 Protocol
==================

节点只依赖抽象协议：sense/dream 走 ``client.complete(prompt, role)``，
act 走 ``client.complete_with_tools(...)``。若 factory 参数写死具体
``MockLlmClient``，节点会伪耦合具体实现——接真 LLM 时所有 factory 签名都要改，
测试 stub 也得靠 ``cast`` 骗过 mypy。用 Protocol 把契约抽象出来后，节点对实现
零依赖。

设计
====

``LlmClient`` 是 ``runtime_checkable Protocol`` —— ``MockLlmClient`` /
Provider client / 测试 stub 都不需要 ``class X(LlmClient):`` 继承声明，只要
符合 ``complete(prompt, *, role) -> dict[str, Any]`` 签名就自动满足。

**runtime_checkable** 的代价：``isinstance(obj, LlmClient)`` 只检查方法存在
不检查签名，这是 typing.Protocol 的限制。对本项目够用——测试已经把签名钉
死了；isinstance 只在 daemon 早期 wiring 兜底用。

**Role 类型** 集中定义在这里，避免节点 / mock / 真 client 各自写 Literal
重复定义。

升级路线
========

接通新 LLM 实现时：建 client 类遵循本 Protocol，cli / daemon factory 选实现
（mock vs 真）——节点代码零改动。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from kindred.llm.tools import ToolEvent, ToolHandler, ToolLoopResult
    from kindred_capability_sdk import ToolDef

# ─────────────────────────────────────────────────────────────────────
# Role 类型 —— 哪个节点在调 LLM
# ─────────────────────────────────────────────────────────────────────

Role = Literal[
    "sense.llm",
    "act.llm",
    "dream.summarize",
    "dream.reflect",
    "dream.gate",
    "dream.excerpt",
]
"""节点角色标识。

- ``sense.llm`` —— T1.sense.llm 节点（决策 act?）
- ``act.llm`` —— T2.act.llm 节点（执行 activity diff）
- ``dream.summarize`` —— dream Step 1.summarize 节点（总结昨日 messages，D6.2）
- ``dream.reflect`` —— dream Step 3.reflect 节点（读灵魂 + 摘要 → diff 提议，D6.4）
- ``dream.gate`` —— dream Step 4.gate 节点（独立闸门检查 changes 是否违规，D6.5）
- ``dream.excerpt`` —— dream Step 5.land 内的 SOUL 有界派生投影

mock client 按 role 返不同 fixture；真 LLM 按 role 选不同 prompt 模板
+ system message。
"""

# ─────────────────────────────────────────────────────────────────────
# LlmClient Protocol
# ─────────────────────────────────────────────────────────────────────


@runtime_checkable
class LlmClient(Protocol):
    """LLM 客户端契约。

    实现侧只需提供 `complete(prompt, *, role)` 方法。``role`` 是
    keyword-only 强制（避免位置参数歧义；测试 stub 必须按 kwarg 调）。

    返回 dict schema 由调用节点验证（``validate_act_decision`` /
    ``_validate_act_output_schema``），本 Protocol 不约束具体字段——给
    mock fixture / 真 LLM 留演化空间，但**所有实现都必须返 dict**（不能返
    list/str/None；坏 schema 在节点边界 raise NodeContractError）。

    使用示例（节点端）::

        def make_sense_llm_node(
            client: LlmClient,  # ← 不再写死 MockLlmClient
        ) -> Callable[[TickState], NodeReturn]:
            ...
            out = client.complete(prompt, role="sense.llm")
            validate_act_decision(out["act_decision"])
            ...

        使用示例（测试 stub）::

        class _StubLlmClient:
            def complete(self, prompt: str, *, role: Role) -> dict[str, Any]:
                return self.fixture_response

        client: LlmClient = _StubLlmClient(...)
    """

    def complete(self, prompt: str, *, role: Role) -> dict[str, Any]:
        """调 LLM 生成内容。

        Args:
            prompt: 已渲染的 prompt 字符串
            role: 节点角色（影响 mock fixture 选择 / 真 LLM 选 system msg）

        Returns:
            dict —— schema 由调用节点验证

        Raises:
            实现可 raise 任意异常；调用节点应将异常包装为
            ``SenseLlmContractError`` / ``ActLlmContractError``（继承
            ``NodeContractError``），让 cli / daemon 一行 catch 兜底。
        """
        ...


class LlmClientError(RuntimeError):
    """LLM 客户端运行时失败基类（earlier review 二轮 N-1）。

    各 Provider error 都继承它，让 daemon / CLI 一行 ``except LlmClientError``
    兜住任意 provider 的运行时错（子进程失败 / HTTP 错 / 坏 JSON等），
    不因 provider 切换漏接、打穿常驻进程。

    fail-fast：raise 即让本 tick 失败；一个心跳失败不致命（下一 tick 再来），但「假装在想」
    会污染状态演化数据。

    ``raw_text``（earlier review review N-1 / step 2）：解析失败时模型的**原始未解析文本**，
    out-of-band 挂在异常上**仅供节点级 gated PromptDumper 落 0600 工件**用。它**绝不进
    ``str(self)`` / 常态日志**——那里只有不含正文的指纹（``role`` + decode 位置 + bytes
    + sha256，见 ``real_client.text_fingerprint``）。因为 client 异常会被 daemon ``%s``
    打进常态日志、CLI 直接 echo，而模型输出可能回显 SOUL / 对话 / prompt 片段。
    """

    def __init__(self, *args: object, raw_text: str | None = None) -> None:
        super().__init__(*args)
        self.raw_text: str | None = raw_text


class ToolLoopError(LlmClientError):
    """有界工具环无法产出 final dict。

    这不是普通 provider 调用失败：工具环里可能已经发生了不可撤的
    ``external_side_effect``（例如 M4 的 ``send_to_user``）。因此异常必须携带完整
    ``tool_events``，M2 的 act 节点捕获后不得直接 raise 掉本 tick，而要构造
    ``committed=False`` 的 ``act_result``：``state_diff_keys=[]``，staged events 不
    apply，已发生的 external side effect 完整进入 ``act_result.tool_trace``，再由
    T3 正常持久化。说出口不可撤，trace 不能丢。
    """

    def __init__(
        self,
        message: str,
        *,
        tool_events: Sequence[ToolEvent],
        rounds: int,
        raw_text: str | None = None,
    ) -> None:
        super().__init__(message, raw_text=raw_text)
        self.tool_events: tuple[ToolEvent, ...] = tuple(tool_events)
        self.rounds = rounds


@runtime_checkable
class ToolCapableLlmClient(LlmClient, Protocol):
    """支持有界工具环的 LLM client。

    独立于基础 ``LlmClient``：sense/dream 仍走单发 ``complete``，act 节点永远走
    ``complete_with_tools``，在 graph 装配期要求本 Protocol。这样不具备工具能力的
    provider fail-fast，而不是静默降级成旧单发路径。
    """

    def complete_with_tools(
        self,
        prompt: str,
        *,
        role: Role,
        tools: Sequence[ToolDef],
        handler: ToolHandler,
        max_rounds: int,
    ) -> ToolLoopResult:
        """运行有界工具环，返回 final dict + 完整 tool trace。"""
        ...


@runtime_checkable
class ManagedLlmClient(LlmClient, Protocol):
    """有生命周期的 LLM 客户端：除 ``complete`` 外还需 ``close()``（earlier milestone）。

    ``build_llm_client`` 工厂返回的 Provider client 都满足（都有 ``complete`` + ``close``）。
    ``daemon._open_client`` 退出时调 ``close()``。

    为什么不把 ``close()`` 并进基础 ``LlmClient``：``MockLlmClient`` 不经工厂、也没有
    ``close``，并进去会让所有「节点接收 LlmClient」的调用点要求 Mock 有 close。故把生命
    周期单列在本 Protocol，只约束工厂产物。``ManagedLlmClient`` 结构上是 ``LlmClient``
    子类型，可直接传给收 ``LlmClient`` 的节点。
    """

    def close(self) -> None:
        """释放底层资源（httpx client / 无）。daemon 退出时调用。"""
        ...


__all__ = [
    "LlmClient",
    "LlmClientError",
    "ManagedLlmClient",
    "Role",
    "ToolCapableLlmClient",
    "ToolLoopError",
]
