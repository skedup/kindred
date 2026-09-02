"""Stable, content-free contracts returned by the telemetry read service."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kindred.telemetry.schema import (
    SAFE_TELEMETRY_TOOL_NAME_PATTERN,
    TELEMETRY_GRAPH_NODE_NAMES,
)

TelemetryWindow = Literal["24h", "7d", "30d"]
TelemetryRunStatus = Literal["running", "succeeded", "failed", "stale"]
TelemetryStoredStatus = Literal["running", "succeeded", "failed"]

_NO_CONTROL_CHARACTERS = r"^[^\x00-\x1f\x7f]+$"
_SAFE_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$"
_UUID4 = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_GRAPH_NODE_NAMES = TELEMETRY_GRAPH_NODE_NAMES
_SAFE_TOOL_NAME = re.compile(SAFE_TELEMETRY_TOOL_NAME_PATTERN)


class TelemetryQueryModel(BaseModel):
    """Strict projection base; arbitrary storage columns never enter API JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TokenTotals(TelemetryQueryModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class LatencyPercentiles(TelemetryQueryModel):
    samples: int = Field(default=0, ge=0)
    p50_us: int | None = Field(default=None, ge=0)
    p95_us: int | None = Field(default=None, ge=0)
    p99_us: int | None = Field(default=None, ge=0)


class UsageCoverage(TelemetryQueryModel):
    request_count: int = Field(default=0, ge=0)
    total_available: int = Field(default=0, ge=0)
    breakdown_available: int = Field(default=0, ge=0)
    total_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    breakdown_ratio: float = Field(default=0.0, ge=0.0, le=1.0)


class UsageBreakdown(TelemetryQueryModel):
    dimension: Literal["role", "provider", "model"]
    key: str = Field(min_length=1, max_length=128, pattern=_SAFE_IDENTIFIER)
    request_count: int = Field(ge=0)
    tokens: TokenTotals


class StageLatency(TelemetryQueryModel):
    name: str = Field(min_length=1, max_length=128, pattern=_NO_CONTROL_CHARACTERS)
    latency: LatencyPercentiles

    @model_validator(mode="after")
    def require_registered_graph_node(self) -> StageLatency:
        if self.name not in _GRAPH_NODE_NAMES:
            raise ValueError("stage name is not a registered graph node")
        return self


class ActRoundBucket(TelemetryQueryModel):
    rounds: int = Field(ge=1)
    run_count: int = Field(ge=1)


class ActMetrics(TelemetryQueryModel):
    round_distribution: list[ActRoundBucket] = Field(default_factory=list)
    input_amplification_mean: float | None = Field(default=None, ge=0.0)


class TelemetryBudgetStatus(TelemetryQueryModel):
    daily_token_warn: int | None = Field(default=None, ge=1)
    daily_token_exceeded: bool | None = None
    tick_duration_warn_seconds: float | None = Field(default=None, gt=0)
    tick_duration_exceeded: bool | None = None
    dream_duration_warn_seconds: float | None = Field(default=None, gt=0)
    dream_duration_exceeded: bool | None = None


class TelemetrySummaryResponse(TelemetryQueryModel):
    schema_version: Literal[1] = 1
    window: TelemetryWindow
    from_at: datetime
    to_at: datetime
    empty: bool
    run_count: int = Field(ge=0)
    succeeded_run_count: int = Field(ge=0)
    failed_run_count: int = Field(ge=0)
    running_run_count: int = Field(ge=0)
    stale_run_count: int = Field(ge=0)
    tokens: TokenTotals
    coverage: UsageCoverage
    run_latency: LatencyPercentiles
    stage_latency: LatencyPercentiles
    stage_latencies: list[StageLatency] = Field(default_factory=list)
    breakdowns: list[UsageBreakdown] = Field(default_factory=list)
    breakdowns_truncated: bool = False
    cache_read_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    failed_spend_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    act: ActMetrics = Field(default_factory=ActMetrics)
    budget: TelemetryBudgetStatus = Field(default_factory=TelemetryBudgetStatus)


class TelemetryRunItem(TelemetryQueryModel):
    run_id: str = Field(pattern=_UUID4)
    run_kind: Literal["tick", "dream"]
    execution_mode: Literal["real", "mock"]
    trigger_source: Literal["cold_start", "heartbeat", "watcher"] | None = None
    dream_date: date | None = None
    tick_id: int | None = Field(default=None, ge=1)
    started_at: datetime
    ended_at: datetime | None = None
    duration_us: int | None = Field(default=None, ge=0)
    status: TelemetryRunStatus
    error_type: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_SAFE_IDENTIFIER,
    )
    app_version: str = Field(min_length=1, max_length=128, pattern=_SAFE_IDENTIFIER)
    span_count: int = Field(default=0, ge=0)
    request_count: int = Field(default=0, ge=0)
    total_available: int = Field(default=0, ge=0)
    tokens: TokenTotals = Field(default_factory=TokenTotals)


class TelemetryRunListResponse(TelemetryQueryModel):
    schema_version: Literal[1] = 1
    empty: bool
    items: list[TelemetryRunItem] = Field(default_factory=list)
    next_cursor: str | None = Field(default=None, max_length=256)


class TelemetryUsageDetail(TelemetryQueryModel):
    usage_source: Literal["provider_complete", "provider_partial", "unavailable"]
    reconciliation_status: Literal[
        "exact", "provider_total_only", "partial", "mismatch", "unavailable"
    ]
    base_input_tokens: int | None = Field(default=None, ge=0)
    visible_output_tokens: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    total_derived: bool
    cache_read_input_tokens: int | None = Field(default=None, ge=0)
    cache_write_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_output_tokens: int | None = Field(default=None, ge=0)
    tool_use_prompt_tokens: int | None = Field(default=None, ge=0)
    unattributed_tokens: int | None = Field(default=None, ge=0)
    provider_cost_microusd: int | None = Field(default=None, ge=0)
    payload_json_bytes: int | None = Field(default=None, ge=0)
    system_text_chars: int | None = Field(default=None, ge=0)
    initial_user_text_chars: int | None = Field(default=None, ge=0)
    tool_schema_json_chars: int | None = Field(default=None, ge=0)
    response_schema_json_chars: int | None = Field(default=None, ge=0)
    model_history_json_chars: int | None = Field(default=None, ge=0)
    tool_result_json_chars: int | None = Field(default=None, ge=0)


class TelemetrySpanDetail(TelemetryQueryModel):
    span_id: str = Field(pattern=_UUID4)
    parent_span_id: str | None = Field(default=None, pattern=_UUID4)
    sequence: int = Field(ge=1)
    span_kind: Literal["graph_node", "llm_request", "tool"]
    name: str = Field(min_length=1, max_length=128, pattern=_NO_CONTROL_CHARACTERS)
    llm_role: (
        Literal[
            "sense.llm",
            "act.llm",
            "dream.summarize",
            "dream.reflect",
            "dream.gate",
            "dream.excerpt",
        ]
        | None
    ) = None
    round_index: int | None = Field(default=None, ge=1)
    source_round_index: int | None = Field(default=None, ge=1)
    tool_effect: (
        Literal[
            "read_only",
            "staged_state_event",
            "external_side_effect",
            "artifact_write",
        ]
        | None
    ) = None
    provider: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_SAFE_IDENTIFIER,
    )
    requested_model: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_SAFE_IDENTIFIER,
    )
    response_model: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_SAFE_IDENTIFIER,
    )
    started_at: datetime
    ended_at: datetime | None = None
    duration_us: int | None = Field(default=None, ge=0)
    status: TelemetryStoredStatus
    error_type: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=_SAFE_IDENTIFIER,
    )
    http_status: int | None = Field(default=None, ge=100, le=599)
    usage: TelemetryUsageDetail | None = None

    @model_validator(mode="after")
    def require_safe_span_name(self) -> TelemetrySpanDetail:
        if self.span_kind == "graph_node":
            if self.name not in _GRAPH_NODE_NAMES:
                raise ValueError("graph span name is not a registered graph node")
        elif self.span_kind == "llm_request":
            if self.llm_role is None or self.name != self.llm_role:
                raise ValueError("LLM span name must equal its registered role")
        elif _SAFE_TOOL_NAME.fullmatch(self.name) is None:
            raise ValueError("tool span name must be a safe identifier")
        return self


class TelemetryStageTree(TelemetryQueryModel):
    stage: TelemetrySpanDetail
    children: list[TelemetrySpanDetail] = Field(default_factory=list)


class TelemetryRunDetailResponse(TelemetryQueryModel):
    schema_version: Literal[1] = 1
    run: TelemetryRunItem
    stages: list[TelemetryStageTree] = Field(default_factory=list)


__all__ = [
    "ActMetrics",
    "ActRoundBucket",
    "LatencyPercentiles",
    "StageLatency",
    "TelemetryBudgetStatus",
    "TelemetryQueryModel",
    "TelemetryRunDetailResponse",
    "TelemetryRunItem",
    "TelemetryRunListResponse",
    "TelemetryRunStatus",
    "TelemetrySpanDetail",
    "TelemetryStageTree",
    "TelemetrySummaryResponse",
    "TelemetryUsageDetail",
    "TelemetryWindow",
    "TokenTotals",
    "UsageBreakdown",
    "UsageCoverage",
]
