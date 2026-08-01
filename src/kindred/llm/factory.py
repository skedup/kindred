"""LLM client factory selected by public Kindred configuration.

- ``provider=claude_code`` → ``ClaudeCodeLlmClient``（走 claude CLI headless，按 claude.ai
  订阅计费）
- ``provider=anthropic`` → ``AnthropicLlmClient``（按 token 走 Messages API，
  ``ANTHROPIC_API_KEY``）
- ``provider=google`` → ``GeminiLlmClient``（走 Google Generative Language API，
  ``GEMINI_API_KEY``/``GOOGLE_API_KEY``，免费 gemini）
- ``provider=deepseek`` → ``DeepSeekLlmClient``（走 DeepSeek Chat Completions，
  ``DEEPSEEK_API_KEY``）
- ``provider=openai`` → ``OpenAILlmClient``（走 OpenAI Responses API，
  ``OPENAI_API_KEY``）
daemon ``_open_client`` 调本工厂；节点代码零改动（只认 ``LlmClient`` Protocol）。
合法取值由 config loader 已校验
（claude_code|anthropic|google|deepseek|openai），
故此处只分支不再校验。

返回 ``ManagedLlmClient``（= ``LlmClient`` + ``close()``）：各 Provider client 都满足；daemon
退出时调 ``close()``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kindred.llm.anthropic_client import AnthropicLlmClient
from kindred.llm.claude_code_client import ClaudeCodeLlmClient
from kindred.llm.deepseek_client import DeepSeekLlmClient
from kindred.llm.gemini_client import GeminiLlmClient
from kindred.llm.openai_client import OpenAILlmClient

if TYPE_CHECKING:
    from kindred.config import KindredConfig
    from kindred.llm.client import ManagedLlmClient


def build_llm_client(config: KindredConfig) -> ManagedLlmClient:
    """据 ``config.llm.provider`` 装配 LLM 客户端。

    Raises:
        ClaudeCodeLlmClientError / AnthropicLlmClientError / GeminiLlmClientError /
        DeepSeekLlmClientError / OpenAILlmClientError:
        凭据/调用失败（由各 client 抛）。
    """
    llm = config.llm
    if llm.provider == "claude_code":
        return ClaudeCodeLlmClient.from_config(llm)
    if llm.provider == "anthropic":
        return AnthropicLlmClient.from_config(llm)
    if llm.provider == "google":
        return GeminiLlmClient.from_config(llm)
    if llm.provider == "deepseek":
        return DeepSeekLlmClient.from_config(llm)
    return OpenAILlmClient.from_config(llm)


__all__ = ["build_llm_client"]
