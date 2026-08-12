"""安装期一次性 Persona 投影；不进入 Heart 运行时 Prompt。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from kindred.resident._contract import PersonaProjection, ResidentInitError

_GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com"
_SYSTEM = (
    "只根据给定 SOUL 与 IDENTITY 生成严格 JSON 运行投影。"
    "soul_excerpt 只从 SOUL 派生，不从 IDENTITY 补充人格事实；"
    "保留稳定核心和会改变反应方式的真实对比面，不用原文没有的常见美德替换具体描述；"
    "舍弃实现说明、系统职责、工具与行动建议，最多 500 字且不追求写满；"
    "traits 包含 openness、agreeableness、conscientiousness、awareness、eros，"
    "每项均为 0..100 整数。不得输出解释或其他字段。"
)


def make_google_persona_projector(
    *, api_key: str, model: str, transport: httpx.BaseTransport | None = None
) -> Callable[[str, str], PersonaProjection]:
    """构造 Google 单次 structured-output projector。"""

    def project(soul: str, identity: str) -> PersonaProjection:
        schema = PersonaProjection.model_json_schema()
        persona = f"<SOUL>\n{soul}\n</SOUL>\n<IDENTITY>\n{identity}\n</IDENTITY>"
        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": _SYSTEM}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": persona}],
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
                "maxOutputTokens": 1024,
            },
        }
        try:
            with httpx.Client(timeout=60, transport=transport) as client:
                response = client.post(
                    f"{_GOOGLE_BASE_URL}/v1beta/models/{model}:generateContent",
                    json=payload,
                    headers={"x-goog-api-key": api_key},
                )
            response.raise_for_status()
            body = response.json()
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            return PersonaProjection.model_validate(json.loads(text))
        except Exception as exc:
            raise ResidentInitError("Persona projection failed") from exc

    return project


__all__ = ["make_google_persona_projector"]
