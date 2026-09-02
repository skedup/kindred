"""Provider usage allowlists and canonical reconciliation."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import replace

from kindred.telemetry.contracts import CanonicalUsage, ReconciliationStatus, UsageSource

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_RAW_URL_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_OPENAI_FINE_TUNED_MODEL = re.compile(
    r"^ft:[A-Za-z0-9][A-Za-z0-9._+-]{0,63}"
    r":[A-Za-z0-9][A-Za-z0-9._+-]{0,63}"
    r":[A-Za-z0-9._+-]{0,63}"
    r":[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"
)
_MAX_SQLITE_INTEGER = 2**63 - 1


def _safe_int(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_SQLITE_INTEGER
    ):
        return None
    return value


def _bounded_sum(*values: int) -> int | None:
    result = sum(values)
    return result if result <= _MAX_SQLITE_INTEGER else None


def _field(value: object, *path: str) -> int | None:
    candidate = value
    for part in path:
        if not isinstance(candidate, dict):
            return None
        candidate = candidate.get(part)
    return _safe_int(candidate)


def safe_response_model(value: object) -> str | None:
    """Return a bounded identifier without trusting arbitrary Provider text."""

    if not isinstance(value, str):
        return None
    if _RAW_URL_PREFIX.match(value) and not _OPENAI_FINE_TUNED_MODEL.fullmatch(value):
        return None
    return value if _SAFE_IDENTIFIER.fullmatch(value) else None


def _canonical(
    *,
    base_input: int | None,
    visible_output: int | None,
    effective_input: int | None,
    effective_output: int | None,
    provider_total: int | None,
    cache_read: int | None = None,
    cache_write: int | None = None,
    reasoning: int | None = None,
    tool_use_prompt: int | None = None,
    provider_cost_microusd: int | None = None,
    allow_derived_total: bool = True,
) -> CanonicalUsage:
    values = (
        base_input,
        visible_output,
        effective_input,
        effective_output,
        provider_total,
        cache_read,
        cache_write,
        reasoning,
        tool_use_prompt,
        provider_cost_microusd,
    )
    if not any(value is not None for value in values):
        return CanonicalUsage.unavailable()

    total = provider_total
    total_derived = False
    unattributed: int | None = None
    reconciliation: ReconciliationStatus
    if effective_input is not None and effective_output is not None:
        known_total = _bounded_sum(effective_input, effective_output)
        if known_total is None:
            reconciliation = "partial" if total is None else "mismatch"
        elif total is None:
            if allow_derived_total:
                total = known_total
                total_derived = True
                reconciliation = "exact"
            else:
                reconciliation = "partial"
        elif total == known_total:
            reconciliation = "exact"
        else:
            known_extra = tool_use_prompt or 0
            remainder = total - known_total - known_extra
            if tool_use_prompt is not None and remainder == 0:
                reconciliation = "partial"
            else:
                reconciliation = "mismatch"
                unattributed = remainder if remainder > 0 else None
    elif total is not None and effective_input is None and effective_output is None:
        reconciliation = "provider_total_only"
    else:
        reconciliation = "partial"

    source: UsageSource = (
        "provider_complete"
        if effective_input is not None and effective_output is not None and total is not None
        else "provider_partial"
    )
    return CanonicalUsage(
        source=source,
        reconciliation_status=reconciliation,
        total_derived=total_derived,
        base_input_tokens=base_input,
        visible_output_tokens=visible_output,
        input_tokens=effective_input,
        output_tokens=effective_output,
        total_tokens=total,
        cache_read_input_tokens=cache_read,
        cache_write_input_tokens=cache_write,
        reasoning_output_tokens=reasoning,
        tool_use_prompt_tokens=tool_use_prompt,
        unattributed_tokens=unattributed,
        provider_cost_microusd=provider_cost_microusd,
    )


def extract_openai_usage(raw: object) -> CanonicalUsage:
    input_tokens = _field(raw, "input_tokens")
    output_tokens = _field(raw, "output_tokens")
    return _canonical(
        base_input=input_tokens,
        visible_output=output_tokens,
        effective_input=input_tokens,
        effective_output=output_tokens,
        provider_total=_field(raw, "total_tokens"),
        cache_read=_field(raw, "input_tokens_details", "cached_tokens"),
        reasoning=_field(raw, "output_tokens_details", "reasoning_tokens"),
    )


def extract_deepseek_usage(raw: object) -> CanonicalUsage:
    input_tokens = _field(raw, "prompt_tokens")
    output_tokens = _field(raw, "completion_tokens")
    return _canonical(
        base_input=input_tokens,
        visible_output=output_tokens,
        effective_input=input_tokens,
        effective_output=output_tokens,
        provider_total=_field(raw, "total_tokens"),
        cache_read=_field(raw, "prompt_cache_hit_tokens"),
        reasoning=_field(raw, "completion_tokens_details", "reasoning_tokens"),
    )


def extract_gemini_usage(raw: object) -> CanonicalUsage:
    input_tokens = _field(raw, "promptTokenCount")
    visible_output = _field(raw, "candidatesTokenCount")
    reasoning = _field(raw, "thoughtsTokenCount")
    effective_output = None
    if visible_output is not None:
        effective_output = _bounded_sum(visible_output, reasoning or 0)
    tool_use = _field(raw, "toolUsePromptTokenCount")
    return _canonical(
        base_input=input_tokens,
        visible_output=visible_output,
        effective_input=input_tokens,
        effective_output=effective_output,
        provider_total=_field(raw, "totalTokenCount"),
        cache_read=_field(raw, "cachedContentTokenCount"),
        reasoning=reasoning,
        tool_use_prompt=tool_use,
        allow_derived_total=tool_use is None,
    )


def extract_anthropic_usage(raw: object) -> CanonicalUsage:
    base_input = _field(raw, "input_tokens")
    cache_read = _field(raw, "cache_read_input_tokens")
    cache_write = _field(raw, "cache_creation_input_tokens")
    output_tokens = _field(raw, "output_tokens")
    effective_input = None
    if base_input is not None:
        effective_input = _bounded_sum(base_input, cache_read or 0, cache_write or 0)
    return _canonical(
        base_input=base_input,
        visible_output=output_tokens,
        effective_input=effective_input,
        effective_output=output_tokens,
        provider_total=_field(raw, "total_tokens"),
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=_field(raw, "output_tokens_details", "thinking_tokens"),
    )


def claude_code_cost_microusd(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        amount = float(value)
    except OverflowError:
        return None
    if not math.isfinite(amount) or amount < 0 or amount > _MAX_SQLITE_INTEGER / 1_000_000:
        return None
    result = round(amount * 1_000_000)
    return result if result <= _MAX_SQLITE_INTEGER else None


def extract_claude_code_usage(envelope: object) -> CanonicalUsage:
    if not isinstance(envelope, dict):
        return CanonicalUsage.unavailable()
    raw = envelope.get("usage")
    cost = claude_code_cost_microusd(envelope.get("total_cost_usd"))
    usage = extract_anthropic_usage(raw)
    if cost is None:
        return usage
    if usage.source == "unavailable":
        return _canonical(
            base_input=None,
            visible_output=None,
            effective_input=None,
            effective_output=None,
            provider_total=None,
            provider_cost_microusd=cost,
        )
    return replace(usage, provider_cost_microusd=cost)


UsageExtractor = Callable[[object], CanonicalUsage]

__all__ = [
    "UsageExtractor",
    "claude_code_cost_microusd",
    "extract_anthropic_usage",
    "extract_claude_code_usage",
    "extract_deepseek_usage",
    "extract_gemini_usage",
    "extract_openai_usage",
    "safe_response_model",
]
