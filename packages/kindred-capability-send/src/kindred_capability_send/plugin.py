"""Portable outbound artifact consumer."""

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

ARTIFACT_FACT = "artifact.explicit_refs.v1"
OUTBOUND_PROFILE = "kindred.compose.outbound.v1"
DELIVERY_FACT_KIND = "send_to_user_delivery"
_DISPATCH_STATUS_KEY = "send_dispatch_status"

SEND_TO_USER = ToolDef(
    name="send_to_user",
    description=(
        "立即且不可撤回地发送 Host 在当前 Activity run 中明确披露、尚未送达的 committed "
        "outbound Artifact。只传 artifact_ref，不传正文；不得猜测 latest、回退 note，或消费"
        "同拍尚未提交的 staged Artifact。没有可用 ref、仍在犹豫或工具失败时，不得伪造已发送。"
    ),
    effect="external_side_effect",
    parameters={
        "type": "OBJECT",
        "properties": {"artifact_ref": {"type": "STRING"}},
        "required": ["artifact_ref"],
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
        raise ValueError("send settings must be empty")

    def handle(call: ToolCall, context: InvocationContext) -> CapabilityResult:
        try:
            ref, status = _visible_artifact(call, context)
        except ValueError as exc:
            return CapabilityResult(
                ToolResult.error(call, error_type="InvalidArtifact", message=str(exc))
            )
        if status == "delivered":
            return CapabilityResult(
                ToolResult.ok(call, {"ok": True, "status": "already_delivered"}),
            )
        if status == "attempted_unknown":
            return CapabilityResult(
                ToolResult.error(
                    call,
                    error_type="AlreadyAttempted",
                    message="message delivery was already attempted and remains unknown",
                )
            )
        dispatch_status = context.transient.get(_DISPATCH_STATUS_KEY)
        if dispatch_status == "delivered":
            return CapabilityResult(
                ToolResult.ok(call, {"ok": True, "status": "already_delivered"}),
            )
        if dispatch_status is not None:
            error_type = "UnknownSideEffect" if dispatch_status == "unknown" else "AlreadyAttempted"
            return CapabilityResult(
                ToolResult.error(
                    call,
                    error_type=error_type,
                    message="message delivery was already attempted in this tick",
                )
            )
        reader = context.artifact_reader
        messenger = context.user_messenger
        if reader is None or messenger is None:
            return CapabilityResult(
                ToolResult.error(
                    call,
                    error_type="HostServiceUnavailable",
                    message="artifact reader or user messenger is unavailable",
                )
            )
        try:
            descriptor = reader.describe_committed(ref)
            if descriptor.artifact_ref != ref or descriptor.profile != OUTBOUND_PROFILE:
                raise ValueError("artifact profile is incompatible")
            content = reader.read_file(ref, "content.md", size_limit=64_000).decode("utf-8").strip()
            if not content:
                raise ValueError("content.md is empty")
        except (UnicodeDecodeError, ValueError):
            return CapabilityResult(
                ToolResult.error(
                    call,
                    error_type="InvalidArtifact",
                    message="outbound artifact is invalid",
                )
            )
        except Exception:
            return CapabilityResult(
                ToolResult.error(
                    call,
                    error_type="ArtifactUnavailable",
                    message="outbound artifact cannot be read",
                )
            )
        context.transient.put(_DISPATCH_STATUS_KEY, "attempted")
        try:
            fact = messenger.send(content, artifact_ref=ref)
        except Exception:
            context.transient.put(_DISPATCH_STATUS_KEY, "unknown")
            return CapabilityResult(
                ToolResult.error(
                    call,
                    error_type="UnknownSideEffect",
                    message="message delivery outcome is unknown",
                )
            )
        if fact.kind != DELIVERY_FACT_KIND or fact.data.get("delivered") is not True:
            context.transient.put(_DISPATCH_STATUS_KEY, "failed")
            fact_error_type = fact.data.get("error_type")
            return CapabilityResult(
                ToolResult.error(
                    call,
                    error_type=(
                        fact_error_type if isinstance(fact_error_type, str) else "DeliveryFailed"
                    ),
                    message="message was not delivered",
                )
            )
        context.transient.put(_DISPATCH_STATUS_KEY, "delivered")
        return CapabilityResult(
            ToolResult.ok(call, {"ok": True, "status": "delivered"}),
            (fact,),
        )

    return CapabilityContribution(
        (ToolBinding(SEND_TO_USER, handle),),
        required_host_services=frozenset({"artifact_reader", "user_messenger"}),
        required_fact_views=frozenset({ARTIFACT_FACT}),
        consumed_artifact_profiles=frozenset({OUTBOUND_PROFILE}),
    )


def _visible_artifact(call: ToolCall, context: InvocationContext) -> tuple[str, str]:
    if set(call.args) != {"artifact_ref"}:
        raise ValueError("send_to_user accepts only artifact_ref")
    ref = call.args.get("artifact_ref")
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("artifact_ref must be a non-empty string")
    fact = context.fact(ARTIFACT_FACT)
    artifacts = fact.value.get("artifacts") if fact is not None else None
    if not isinstance(artifacts, (tuple, list)):
        raise ValueError("artifact fact is unavailable")
    for item in artifacts:
        if (
            isinstance(item, Mapping)
            and item.get("artifact_ref") == ref
            and item.get("profile") == OUTBOUND_PROFILE
        ):
            status = item.get("status")
            return (
                ref,
                status
                if status in {"available", "delivered", "attempted_unknown"}
                else "available",
            )
    raise ValueError("artifact_ref is not committed for the current activity")


__all__ = ["OUTBOUND_PROFILE", "SEND_TO_USER", "create_capability"]
