"""xAI Responses API client.

The xAI text endpoint intentionally shares the already-tested Responses API
wire loop with OpenAI.  This module owns only xAI's credential, endpoint and
payload differences; model IDs remain runtime configuration.
"""

from __future__ import annotations

from kindred.llm.client import LlmClientError
from kindred.llm.openai_client import OpenAILlmClient


class XaiLlmClientError(LlmClientError):
    pass


class XaiLlmClient(OpenAILlmClient):
    """Direct, non-streaming xAI client for ``POST /v1/responses``."""

    _PROVIDER_NAME = "xAI"
    _CLIENT_NAME = "XaiLlmClient"
    _CLIENT_ERROR = XaiLlmClientError
    _API_KEY_ENV = "XAI_API_KEY"
    _BASE_URL_ENV = "XAI_BASE_URL"
    _DEFAULT_BASE_URL = "https://api.x.ai/v1"
    _DEFAULT_TIMEOUT_S = 300.0
    # xAI documents ``text.format`` but not OpenAI's verbosity extension.
    _INCLUDE_TEXT_VERBOSITY = False
    # xAI function schemas are strict implicitly and its documented wire omits this field.
    _INCLUDE_TOOL_STRICT = False
    # xAI stateless tool loops require encrypted reasoning items to continue coherently.
    _INCLUDE_ENCRYPTED_REASONING = True


__all__ = ["XaiLlmClient", "XaiLlmClientError"]
