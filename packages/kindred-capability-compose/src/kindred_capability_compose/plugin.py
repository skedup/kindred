"""Portable compose artifact producer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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

ACTIVITY_FACT = "activity.current"
OUTBOUND_PROFILE = "kindred.compose.outbound.v1"
_SAFE_SOURCE_REF_KINDS = frozenset({"feed", "memory", "plain", "tick"})
_RISK_TERMS = (
    "测试",
    "探针",
    "冒烟",
    "灰度",
    "debug",
    "ai",
    "agent",
    "llm",
    "mcp",
    "模型",
    "自动化",
    "脚本",
    "工具调用",
)

WRITE_COMPOSE = ToolDef(
    name="write_compose",
    description=(
        "为当前活动起草作品并暂存为 Artifact；只有本拍最终 committed=true 才会正式提交。"
        "落点由 Host 根据当前活动推导，不传路由字段；正式 artifact_ref 从后续 tick 起才可被"
        "显式消费。正文、标题、语气和来源必须来自当前活动的真实材料。"
    ),
    effect="artifact_write",
    parameters={
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING", "nullable": True},
            "content": {"type": "STRING"},
            "source_refs": {
                "type": "ARRAY",
                "nullable": True,
                "items": {"type": "STRING"},
            },
            "tone": {"type": "STRING", "nullable": True},
        },
        "required": ["content"],
        "additionalProperties": False,
    },
)


def create_capability(
    *,
    settings: Mapping[str, Any],
    secrets: SecretResolver,
) -> CapabilityContribution:
    del secrets
    if settings:
        raise ValueError("compose settings must be empty")

    def handle(call: ToolCall, context: InvocationContext) -> CapabilityResult:
        try:
            content, title, source_refs, tone = _parse_args(call.args)
            profile = _profile_for_activity(context)
        except ValueError as exc:
            return CapabilityResult(
                ToolResult.error(call, error_type="InvalidRequest", message=str(exc))
            )
        writer = context.artifact_writer
        if writer is None:
            return CapabilityResult(
                ToolResult.error(
                    call,
                    error_type="ArtifactStoreUnavailable",
                    message="artifact writer is unavailable",
                )
            )
        files = {"content.md": content}
        try:
            writer.stage_bundle(profile, files)
        except Exception:
            return CapabilityResult(
                ToolResult.error(
                    call,
                    error_type="ArtifactWriteFailed",
                    message="compose artifact could not be staged",
                )
            )
        risk_flags = _risk_flags(title, content)
        return CapabilityResult(
            ToolResult.ok(
                call,
                {
                    "ok": True,
                    "status": "staged",
                    "profile": profile,
                    "content_len": len(content),
                    "title_len": len(title) if title is not None else None,
                    "source_refs_count": len(source_refs),
                    "source_ref_kinds": _source_ref_kinds(source_refs),
                    "tone_present": tone is not None,
                    "risk_flags": list(risk_flags),
                },
            )
        )

    return CapabilityContribution(
        (ToolBinding(WRITE_COMPOSE, handle),),
        required_host_services=frozenset({"artifact_writer"}),
        required_fact_views=frozenset({ACTIVITY_FACT}),
        produced_artifact_profiles=frozenset({OUTBOUND_PROFILE}),
    )


def _parse_args(args: Mapping[str, Any]) -> tuple[str, str | None, tuple[str, ...], str | None]:
    if set(args) - {"title", "content", "source_refs", "tone"}:
        raise ValueError("unexpected compose arguments")
    content = args.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty string")
    title = _optional_text(args.get("title"))
    tone = _optional_text(args.get("tone"))
    raw_refs = args.get("source_refs")
    if raw_refs is None:
        refs: tuple[str, ...] = ()
    elif isinstance(raw_refs, list) and all(isinstance(item, str) for item in raw_refs):
        refs = tuple(item.strip() for item in raw_refs if item.strip())[:10]
    else:
        raise ValueError("source_refs must be a list of strings")
    return content.strip(), title, refs, tone


def _profile_for_activity(context: InvocationContext) -> str:
    fact = context.fact(ACTIVITY_FACT)
    raw = fact.value.get("capabilities") if fact is not None else None
    capabilities = frozenset(raw) if isinstance(raw, (tuple, list)) else frozenset()
    if "send" in capabilities:
        return OUTBOUND_PROFILE
    raise ValueError("current activity does not define an outbound compose route")


def _optional_text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _source_ref_kinds(refs: tuple[str, ...]) -> list[str]:
    kinds: list[str] = []
    for ref in refs:
        prefix = ref.split(":", 1)[0].strip().lower() if ":" in ref else "plain"
        kind = prefix if prefix in _SAFE_SOURCE_REF_KINDS else "other"
        if kind not in kinds:
            kinds.append(kind)
    return kinds


def _risk_flags(title: str | None, content: str) -> tuple[str, ...]:
    text = f"{title or ''}\n{content}".lower()
    return (
        ("platform_visible_engineering_or_test_terms",)
        if any(term in text for term in _RISK_TERMS)
        else ()
    )


__all__ = ["OUTBOUND_PROFILE", "WRITE_COMPOSE", "create_capability"]
