"""大模型调用层。

参考文档：docs/14-heart-graph.md §3.3 / §3.4（prompt 骨架）

依赖：（无外部依赖）

模块：
- client.py             LlmClient / ToolCapableLlmClient / ManagedLlmClient Protocol + Role
- mock.py               Deterministic LLM client——集成测试用
- tools.py              ToolDef / ToolCall / ToolResult / ToolLoopResult（工具环中立协议）
- real_client.py        Provider-neutral shared system prompt and JSON parser
- claude_code_client.py ClaudeCodeLlmClient——走 claude CLI headless，按订阅计费
- anthropic_client.py    AnthropicLlmClient——按 token 走 Messages API
- gemini_client.py       GeminiLlmClient——走 Google Generative Language API（免费 gemini）
- deepseek_client.py     DeepSeekLlmClient——走 DeepSeek Chat Completions + Tool Calls
- openai_client.py       OpenAILlmClient——走 OpenAI Responses API + Function Calling
- xai_client.py          XaiLlmClient——走 xAI Responses API + Function Calling
- factory.py            build_llm_client——按 config 选 provider
- prompts/              jinja2 模板（progressive disclosure，按需加载）
"""

from __future__ import annotations

from kindred.llm.anthropic_client import AnthropicLlmClient, AnthropicLlmClientError
from kindred.llm.claude_code_client import (
    ClaudeCodeLlmClient,
    ClaudeCodeLlmClientError,
)
from kindred.llm.client import (
    LlmClient,
    ManagedLlmClient,
    Role,
    ToolCapableLlmClient,
    ToolLoopError,
)
from kindred.llm.deepseek_client import (
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_DEEPSEEK_REASONING_EFFORT,
    DeepSeekLlmClient,
    DeepSeekLlmClientError,
)
from kindred.llm.factory import build_llm_client
from kindred.llm.gemini_client import GeminiLlmClient, GeminiLlmClientError
from kindred.llm.mock import MockLlmClient, MockToolRound, MockToolScript, Scenario
from kindred.llm.openai_client import OpenAILlmClient, OpenAILlmClientError
from kindred.llm.tools import (
    ToolEvent,
    ToolLoopResult,
)
from kindred.llm.xai_client import XaiLlmClient, XaiLlmClientError

__all__ = [
    "AnthropicLlmClient",
    "AnthropicLlmClientError",
    "ClaudeCodeLlmClient",
    "ClaudeCodeLlmClientError",
    "DEFAULT_DEEPSEEK_MODEL",
    "DEFAULT_DEEPSEEK_REASONING_EFFORT",
    "DeepSeekLlmClient",
    "DeepSeekLlmClientError",
    "GeminiLlmClient",
    "GeminiLlmClientError",
    "LlmClient",
    "ManagedLlmClient",
    "MockLlmClient",
    "MockToolRound",
    "MockToolScript",
    "OpenAILlmClient",
    "OpenAILlmClientError",
    "Role",
    "Scenario",
    "ToolCapableLlmClient",
    "ToolEvent",
    "ToolLoopError",
    "ToolLoopResult",
    "XaiLlmClient",
    "XaiLlmClientError",
    "build_llm_client",
]
