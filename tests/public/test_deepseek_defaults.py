"""Public-safe DeepSeek default request contract."""

from __future__ import annotations

import json
from typing import Any

import httpx

from kindred.llm import DEFAULT_DEEPSEEK_MODEL, DEFAULT_DEEPSEEK_REASONING_EFFORT
from kindred.llm.deepseek_client import DeepSeekLlmClient


def test_deepseek_defaults_use_pro_with_high_reasoning() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"ok":true}'},
                    }
                ]
            },
        )

    client = DeepSeekLlmClient(
        api_key="EXAMPLE_API_KEY",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.complete("synthetic input", role="sense.llm") == {"ok": True}
    finally:
        client.close()

    assert captured["model"] == DEFAULT_DEEPSEEK_MODEL == "deepseek-v4-pro"
    assert captured["thinking"] == {"type": "enabled"}
    assert captured["reasoning_effort"] == DEFAULT_DEEPSEEK_REASONING_EFFORT == "high"
