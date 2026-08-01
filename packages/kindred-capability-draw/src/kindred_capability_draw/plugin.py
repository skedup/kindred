"""Portable Draw capability factory."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Literal, cast

import httpx
from kindred_capability_sdk import (
    CapabilityContribution,
    CapabilityResult,
    InvocationContext,
    SecretResolver,
    ToolBinding,
    ToolCall,
    ToolDef,
    ToolResult,
)

from .models import (
    DrawSettings,
    GeneratedImage,
    ImageProvider,
    ImageProviderError,
    ImageProviderRejected,
    ImageRequest,
    Layout,
)
from .provider import GoogleImageProvider, OpenAIImageProvider

DRAW_PROFILE = "kindred.draw.image.v1"

_MAX_PROMPT_CHARS = 4_000
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_ATTEMPT_KEY = "generation_attempted"
_LAYOUTS = frozenset({"portrait", "square", "landscape"})
_DRAW_TOOL = ToolDef(
    name="draw_image",
    description="根据当前明确构思生成一张 PNG 图片；layout 可选 portrait、square 或 landscape。",
    parameters={
        "type": "OBJECT",
        "properties": {
            "prompt": {"type": "STRING"},
            "layout": {
                "type": "STRING",
                "enum": ["portrait", "square", "landscape"],
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
    effect="artifact_write",
)


def create_capability(
    *,
    settings: Mapping[str, Any],
    secrets: SecretResolver,
) -> CapabilityContribution:
    parsed = _parse_settings(settings)
    api_key = secrets.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("draw api_key secret is required")
    provider = _build_provider(parsed, api_key.strip())

    def draw(call: ToolCall, context: InvocationContext) -> CapabilityResult:
        request = _parse_request(call)
        if request is None:
            return _error(call, "InvalidRequest", "draw request is invalid")
        writer = context.artifact_writer
        if writer is None:
            return _error(
                call,
                "ArtifactStoreUnavailable",
                "artifact writer is unavailable",
            )
        if context.transient.get(_ATTEMPT_KEY, False):
            return _error(
                call,
                "GenerationBudgetExceeded",
                "one draw attempt is allowed per tick",
            )
        context.transient.put(_ATTEMPT_KEY, True)
        try:
            image = provider.generate(request)
            _validate_image(image)
        except httpx.TimeoutException:
            return _error(call, "ProviderTimeout", "draw provider timed out")
        except ImageProviderRejected:
            return _error(call, "ProviderRejected", "draw provider returned no usable image")
        except Exception:
            return _error(call, "ProviderError", "draw provider failed")
        try:
            writer.stage_bundle(DRAW_PROFILE, {"image.png": image.content})
        except Exception:
            return _error(
                call,
                "ArtifactWriteFailed",
                "image artifact could not be staged",
            )
        return CapabilityResult(
            ToolResult.ok(
                call,
                {
                    "ok": True,
                    "status": "staged",
                    "format": "png",
                    "layout": request.layout,
                },
            )
        )

    return CapabilityContribution(
        tool_bindings=(ToolBinding(_DRAW_TOOL, draw),),
        required_host_services=frozenset({"artifact_writer"}),
        produced_artifact_profiles=frozenset({DRAW_PROFILE}),
        close=provider.close,
    )


def _parse_settings(settings: Mapping[str, Any]) -> DrawSettings:
    unknown = set(settings) - {"provider", "model", "timeout_s"}
    if unknown:
        raise ValueError(f"unknown draw settings: {sorted(unknown)}")
    provider = settings.get("provider")
    if not isinstance(provider, str) or provider not in {"google", "openai"}:
        raise ValueError("draw provider must be google or openai")
    model = settings.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("draw model must be a non-empty string")
    timeout = settings.get("timeout_s", 120)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 10 <= timeout <= 300
    ):
        raise ValueError("draw timeout_s must be a number from 10 to 300")
    return DrawSettings(
        cast("Literal['google', 'openai']", provider),
        model.strip(),
        float(timeout),
    )


def _parse_request(call: ToolCall) -> ImageRequest | None:
    if set(call.args) - {"prompt", "layout"}:
        return None
    prompt = call.args.get("prompt")
    if not isinstance(prompt, str):
        return None
    prompt = prompt.strip()
    if not prompt or len(prompt) > _MAX_PROMPT_CHARS:
        return None
    layout = call.args.get("layout", "portrait")
    if not isinstance(layout, str) or layout not in _LAYOUTS:
        return None
    return ImageRequest(prompt, cast("Layout", layout))


def _build_provider(settings: DrawSettings, api_key: str) -> ImageProvider:
    provider_type = GoogleImageProvider if settings.provider == "google" else OpenAIImageProvider
    return provider_type(
        api_key=api_key,
        model=settings.model,
        timeout_s=settings.timeout_s,
    )


def _validate_image(image: GeneratedImage) -> None:
    if (
        image.media_type != "image/png"
        or not image.content.startswith(_PNG_SIGNATURE)
        or len(image.content) > _MAX_IMAGE_BYTES
    ):
        raise ImageProviderError


def _error(call: ToolCall, error_type: str, message: str) -> CapabilityResult:
    return CapabilityResult(ToolResult.error(call, error_type=error_type, message=message))


__all__ = ["DRAW_PROFILE", "create_capability"]
