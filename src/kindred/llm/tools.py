"""LLM tool-loop 的执行协议。

公开的 ToolDef / ToolCall / ToolResult / ToolEffect 由独立
``kindred_capability_sdk`` wheel 唯一定义；本模块只保留 Host 工具环自身的
event、handler 与渲染 helper。

工具效果语义四分类来自
``docs/discussions/2026-07-04-activity-as-skill-execution.md`` §3.2：
state 变更类工具只能暂存、交给 F1 原子提交；只有外部副作用类工具在环内不可撤地
真执行。这里把 effect 放进 ToolDef，是为了让后续 act 节点和日志/trace 不靠名字猜语义。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from kindred_capability_sdk import ToolCall as _ToolCall
from kindred_capability_sdk import ToolDef as _ToolDef
from kindred_capability_sdk import ToolEffect as _ToolEffect
from kindred_capability_sdk import ToolResult as _ToolResult


@dataclass(frozen=True)
class ToolEvent:
    """一次工具调用及其结果，按发生顺序进入 ``ToolLoopResult.tool_events``。"""

    round_index: int
    call: _ToolCall
    result: _ToolResult
    effect: _ToolEffect | None = None


@dataclass(frozen=True)
class ToolLoopResult:
    """有界工具环的最终结果。"""

    final: dict[str, Any]
    tool_events: tuple[ToolEvent, ...]
    rounds: int


class ToolHandler(Protocol):
    """调用侧注入的工具执行函数。"""

    def __call__(self, call: _ToolCall) -> _ToolResult:
        """执行工具调用并返回结构化结果。"""
        ...


def call_tool_handler(handler: ToolHandler, call: _ToolCall) -> _ToolResult:
    """执行 handler；handler 抛错时包成 error ToolResult 回灌给模型。

    异常 message 可能含敏感上下文，这里只回灌类型与固定说明。真正业务 handler
    若想给模型更细错误，可主动返回 ``ToolResult(is_error=True, ...)``。
    """

    try:
        result = handler(call)
    except Exception as exc:  # noqa: BLE001 - tool-loop contract: errors become ToolResult
        return _ToolResult.error(
            call,
            error_type=type(exc).__name__,
            message="handler raised",
        )
    if result.call_id is None and call.call_id is not None:
        return result.with_call_id(call.call_id)
    return result


def effect_for_tool(tools: dict[str, _ToolDef], name: str) -> _ToolEffect | None:
    tool = tools.get(name)
    return tool.effect if tool is not None else None


def render_tool_contract(tools: list[_ToolDef] | tuple[_ToolDef, ...]) -> str:
    """把 ToolDef 渲成给测试/文档用的简短契约文本。

    这不是生产 prompt，只是把「工具声明来自 ToolDef」机械钉住，延续
    ``test_schema_sync`` 的同源思路。
    """

    lines: list[str] = []
    for tool in tools:
        required = tool.parameters.get("required")
        required_text = (
            ", ".join(required)
            if isinstance(required, Sequence) and not isinstance(required, str)
            else "-"
        )
        properties = tool.parameters.get("properties")
        if isinstance(properties, Mapping):
            property_text = ", ".join(properties)
        else:
            property_text = "-"
        lines.append(
            f"- {tool.name} ({tool.effect}): {tool.description} "
            f"[properties: {property_text}; required: {required_text}]"
        )
    return "\n".join(lines)


def tool_map(tools: list[_ToolDef] | tuple[_ToolDef, ...]) -> dict[str, _ToolDef]:
    """按 name 建索引，重复工具名 fail-fast。"""

    indexed: dict[str, _ToolDef] = {}
    for tool in tools:
        if tool.name in indexed:
            raise ValueError(f"duplicate tool name: {tool.name}")
        indexed[tool.name] = tool
    return indexed


__all__ = [
    "ToolEvent",
    "ToolHandler",
    "ToolLoopResult",
    "call_tool_handler",
    "effect_for_tool",
    "render_tool_contract",
    "tool_map",
]
