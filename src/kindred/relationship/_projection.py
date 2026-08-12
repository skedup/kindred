"""Install-time USER-only structured Relationship projection."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from kindred.relationship.models import RelationshipBootstrap

_GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com"
_OPENAI_BASE_URL = "https://api.openai.com/v1"
_SYSTEM = (
    "只根据给定 USER 文档投影 ta 当前如何看待这个 user。输出严格 JSON："
    "declared_role 只能是 unlabeled、friend、lover、hostile，叙事不明确时用 unlabeled；"
    "trust、attachment、attraction、friction 只能是 none、low、medium、high；"
    "summary 用一句不引用原文、不含姓名或私密细节的中文定性摘要。"
    "不要输出精确数值、推理过程或其他字段。"
)


def _validate_bootstrap(text: str) -> RelationshipBootstrap:
    return RelationshipBootstrap.model_validate(json.loads(text))


def make_google_relationship_projector(
    *,
    api_key: str,
    model: str,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Callable[[str], RelationshipBootstrap]:
    """Build the single install-time structured projector for an existing USER."""

    def project(user_text: str) -> RelationshipBootstrap:
        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": _SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": f"<USER>\n{user_text}\n</USER>"}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": RelationshipBootstrap.model_json_schema(),
                "maxOutputTokens": 512,
            },
        }
        try:
            with httpx.Client(timeout=60, transport=transport) as client:
                response = client.post(
                    f"{(base_url or _GOOGLE_BASE_URL).rstrip('/')}/v1beta/models/"
                    f"{model}:generateContent",
                    json=payload,
                    headers={"x-goog-api-key": api_key},
                )
            response.raise_for_status()
            body = response.json()
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            return _validate_bootstrap(text)
        except Exception as exc:
            raise RuntimeError("relationship bootstrap projection failed") from exc

    return project


def make_openai_relationship_projector(
    *,
    api_key: str,
    model: str,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Callable[[str], RelationshipBootstrap]:
    """Build the USER-only projector for the configured OpenAI Responses API."""

    def project(user_text: str) -> RelationshipBootstrap:
        payload: dict[str, Any] = {
            "model": model,
            "input": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"<USER>\n{user_text}\n</USER>"},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "relationship_bootstrap",
                    "strict": True,
                    "schema": RelationshipBootstrap.model_json_schema(),
                },
                "verbosity": "low",
            },
            "reasoning": {"effort": "high"},
            "max_output_tokens": 512,
            "store": False,
            "stream": False,
        }
        try:
            with httpx.Client(timeout=60, transport=transport) as client:
                response = client.post(
                    f"{(base_url or _OPENAI_BASE_URL).rstrip('/')}/responses",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
            response.raise_for_status()
            body = response.json()
            if body.get("status") != "completed":
                raise ValueError("response is not completed")
            messages = [item for item in body["output"] if item.get("type") == "message"]
            if len(messages) != 1 or messages[0].get("role") != "assistant":
                raise ValueError("response has no unique assistant message")
            content = messages[0]["content"]
            if len(content) != 1 or content[0].get("type") != "output_text":
                raise ValueError("response has no unique output text")
            return _validate_bootstrap(content[0]["text"])
        except Exception as exc:
            raise RuntimeError("relationship bootstrap projection failed") from exc

    return project
