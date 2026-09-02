"""Strong, content-free contracts for local Kindred telemetry."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Protocol, TypeAlias, TypeVar
from uuid import UUID, uuid4

from kindred.telemetry.schema import (
    SAFE_TELEMETRY_TOOL_NAME_PATTERN,
    TELEMETRY_GRAPH_NODE_NAMES,
    TELEMETRY_LLM_ROLES,
)
from kindred_capability_sdk import ToolEffect

RunKind: TypeAlias = Literal["tick", "dream"]
ExecutionMode: TypeAlias = Literal["real", "mock"]
TriggerSource: TypeAlias = Literal["cold_start", "heartbeat", "watcher"]
FinalStatus: TypeAlias = Literal["succeeded", "failed"]
CompletedSpanKind: TypeAlias = Literal["graph_node", "tool"]
UsageSource: TypeAlias = Literal["provider_complete", "provider_partial", "unavailable"]
ReconciliationStatus: TypeAlias = Literal[
    "exact", "provider_total_only", "partial", "mismatch", "unavailable"
]
LlmRole: TypeAlias = Literal[
    "sense.llm",
    "act.llm",
    "dream.summarize",
    "dream.reflect",
    "dream.gate",
    "dream.excerpt",
]

_RUN_KINDS = frozenset({"tick", "dream"})
_EXECUTION_MODES = frozenset({"real", "mock"})
_TRIGGER_SOURCES = frozenset({"cold_start", "heartbeat", "watcher"})
_FINAL_STATUSES = frozenset({"succeeded", "failed"})
_COMPLETED_SPAN_KINDS = frozenset({"graph_node", "tool"})
_ROLES = TELEMETRY_LLM_ROLES
_GRAPH_NODE_NAMES = TELEMETRY_GRAPH_NODE_NAMES
_TOOL_EFFECTS = frozenset(
    {"read_only", "staged_state_event", "external_side_effect", "artifact_write"}
)
_USAGE_SOURCES = frozenset({"provider_complete", "provider_partial", "unavailable"})
_RECONCILIATION_STATUSES = frozenset(
    {"exact", "provider_total_only", "partial", "mismatch", "unavailable"}
)
_SAFE_TOOL_NAME = re.compile(SAFE_TELEMETRY_TOOL_NAME_PATTERN)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_TOOL_ERROR_TYPES = frozenset({"ToolHandlerException", "ToolResultError", "UnknownTool"})
_CaptureT = TypeVar("_CaptureT")


def new_trace_id() -> str:
    """Return a canonical lowercase UUID4 for a run or span."""

    return str(uuid4())


def validate_trace_id(value: str, field: str = "trace_id") -> None:
    """Validate a public run/span identifier at a context boundary."""

    _require_uuid4(value, field)


def utc_now() -> datetime:
    """Return an aware UTC wall-clock timestamp."""

    return datetime.now(tz=timezone.utc)


def _require_uuid4(value: str, field: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string UUID4")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a canonical UUID4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{field} must be a canonical lowercase UUID4")


def _require_utc(value: datetime, field: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC-aware")


def _require_text(value: str, field: str, *, maximum: int = 128) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not value or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{field} must be non-empty, trimmed, and <= {maximum} chars")


def _require_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} must be a safe bounded identifier")


def _require_non_negative(value: int | None, field: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError(f"{field} must be a non-negative integer or None")


def _require_positive(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")


@dataclass(frozen=True)
class RunStart:
    run_id: str
    run_kind: RunKind
    execution_mode: ExecutionMode
    started_at: datetime
    app_version: str
    trigger_source: TriggerSource | None = None
    dream_date: date | None = None

    def __post_init__(self) -> None:
        _require_uuid4(self.run_id, "run_id")
        if self.run_kind not in _RUN_KINDS:
            raise ValueError("run_kind must be tick or dream")
        if self.execution_mode not in _EXECUTION_MODES:
            raise ValueError("execution_mode must be real or mock")
        _require_utc(self.started_at, "started_at")
        _require_identifier(self.app_version, "app_version")
        if self.trigger_source is not None and self.trigger_source not in _TRIGGER_SOURCES:
            raise ValueError("invalid trigger_source")
        if self.run_kind == "tick":
            if self.trigger_source is None or self.dream_date is not None:
                raise ValueError("tick run requires trigger_source and forbids dream_date")
        elif self.trigger_source is not None or type(self.dream_date) is not date:
            raise ValueError("dream run requires dream_date and forbids trigger_source")


@dataclass(frozen=True)
class StageStart:
    span_id: str
    run_id: str
    sequence: int
    name: str
    started_at: datetime

    def __post_init__(self) -> None:
        _require_uuid4(self.span_id, "span_id")
        _require_uuid4(self.run_id, "run_id")
        _require_positive(self.sequence, "sequence")
        if self.name not in _GRAPH_NODE_NAMES:
            raise ValueError("stage name must be a registered graph node constant")
        _require_utc(self.started_at, "started_at")


@dataclass(frozen=True)
class PromptShape:
    payload_json_bytes: int | None = None
    system_text_chars: int | None = None
    initial_user_text_chars: int | None = None
    tool_schema_json_chars: int | None = None
    response_schema_json_chars: int | None = None
    model_history_json_chars: int | None = None
    tool_result_json_chars: int | None = None

    def __post_init__(self) -> None:
        for item in fields(self):
            _require_non_negative(getattr(self, item.name), item.name)


@dataclass(frozen=True)
class LlmRequestStart:
    span_id: str
    run_id: str
    parent_stage: StageStart
    sequence: int
    llm_role: LlmRole
    round_index: int
    provider: str
    requested_model: str
    started_at: datetime

    def __post_init__(self) -> None:
        _require_uuid4(self.span_id, "span_id")
        _require_uuid4(self.run_id, "run_id")
        if not isinstance(self.parent_stage, StageStart):
            raise TypeError("parent_stage must be a StageStart")
        if self.parent_stage.run_id != self.run_id:
            raise ValueError("parent stage must belong to request run")
        _require_positive(self.sequence, "sequence")
        if self.sequence <= self.parent_stage.sequence:
            raise ValueError("request sequence must follow parent stage sequence")
        if self.llm_role not in _ROLES:
            raise ValueError("invalid llm_role")
        _require_positive(self.round_index, "round_index")
        _require_identifier(self.provider, "provider")
        _require_identifier(self.requested_model, "requested_model")
        _require_utc(self.started_at, "started_at")


@dataclass(frozen=True)
class CanonicalUsage:
    source: UsageSource
    reconciliation_status: ReconciliationStatus
    total_derived: bool = False
    base_input_tokens: int | None = None
    visible_output_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    tool_use_prompt_tokens: int | None = None
    unattributed_tokens: int | None = None
    provider_cost_microusd: int | None = None

    def __post_init__(self) -> None:
        if self.source not in _USAGE_SOURCES:
            raise ValueError("invalid usage source")
        if self.reconciliation_status not in _RECONCILIATION_STATUSES:
            raise ValueError("invalid reconciliation status")
        if type(self.total_derived) is not bool:
            raise TypeError("total_derived must be a bool")
        numeric_names = tuple(item.name for item in fields(self))[3:]
        for name in numeric_names:
            _require_non_negative(getattr(self, name), name)
        numeric_values = tuple(getattr(self, name) for name in numeric_names)
        if self.source == "unavailable":
            if any(value is not None for value in numeric_values):
                raise ValueError("unavailable usage cannot contain provider values")
            if self.total_derived or self.reconciliation_status != "unavailable":
                raise ValueError("unavailable usage must use unavailable reconciliation")
        elif self.reconciliation_status == "unavailable":
            raise ValueError("provider usage cannot use unavailable reconciliation")
        elif not any(value is not None for value in numeric_values):
            raise ValueError("provider usage requires at least one safe numeric value")
        if self.source == "provider_complete" and (
            self.input_tokens is None or self.output_tokens is None or self.total_tokens is None
        ):
            raise ValueError("complete provider usage requires input/output/total")
        if self.total_derived and self.total_tokens is None:
            raise ValueError("derived total requires total_tokens")
        if self.total_derived:
            if self.reconciliation_status != "exact":
                raise ValueError("derived total requires exact reconciliation")
            if self.tool_use_prompt_tokens is not None:
                raise ValueError("derived total cannot omit tool-use prompt tokens")
            if self.input_tokens is None or self.output_tokens is None:
                raise ValueError("derived total requires effective input/output")
            if self.total_tokens != self.input_tokens + self.output_tokens:
                raise ValueError("derived total must equal effective input plus output")
        if self.reconciliation_status == "exact":
            if (
                self.input_tokens is None
                or self.output_tokens is None
                or self.total_tokens is None
                or self.total_tokens != self.input_tokens + self.output_tokens
                or self.unattributed_tokens is not None
            ):
                raise ValueError("exact usage must reconcile effective input/output/total")
        if self.reconciliation_status == "provider_total_only" and (
            self.total_tokens is None
            or self.input_tokens is not None
            or self.output_tokens is not None
            or self.total_derived
        ):
            raise ValueError("provider-total-only usage must contain only the provider total")

    @classmethod
    def unavailable(cls) -> CanonicalUsage:
        return cls(source="unavailable", reconciliation_status="unavailable")


@dataclass(frozen=True)
class LlmRequestFinish:
    span_id: str
    run_id: str
    ended_at: datetime
    duration_us: int
    status: FinalStatus
    usage: CanonicalUsage
    prompt_shape: PromptShape = PromptShape()
    response_model: str | None = None
    error_type: str | None = None
    http_status: int | None = None

    def __post_init__(self) -> None:
        _require_uuid4(self.span_id, "span_id")
        _require_uuid4(self.run_id, "run_id")
        _require_utc(self.ended_at, "ended_at")
        _require_non_negative(self.duration_us, "duration_us")
        if self.status not in _FINAL_STATUSES:
            raise ValueError("request status must be succeeded or failed")
        if self.status == "succeeded" and self.error_type is not None:
            raise ValueError("succeeded request cannot have error_type")
        if self.status == "failed" and self.error_type is not None:
            _require_identifier(self.error_type, "error_type")
        if not isinstance(self.usage, CanonicalUsage):
            raise TypeError("usage must be CanonicalUsage")
        if not isinstance(self.prompt_shape, PromptShape):
            raise TypeError("prompt_shape must be a PromptShape")
        if self.response_model is not None:
            _require_identifier(self.response_model, "response_model")
        if self.http_status is not None and (
            isinstance(self.http_status, bool)
            or not isinstance(self.http_status, int)
            or not 100 <= self.http_status <= 599
        ):
            raise ValueError("http_status must be an HTTP status or None")


@dataclass(frozen=True)
class CompletedSpan:
    span_id: str
    run_id: str
    sequence: int
    span_kind: CompletedSpanKind
    name: str
    started_at: datetime
    ended_at: datetime
    duration_us: int
    status: FinalStatus
    parent_span_id: str | None = None
    source_round_index: int | None = None
    tool_effect: ToolEffect | None = None
    error_type: str | None = None

    def __post_init__(self) -> None:
        _require_uuid4(self.span_id, "span_id")
        _require_uuid4(self.run_id, "run_id")
        _require_positive(self.sequence, "sequence")
        if self.span_kind not in _COMPLETED_SPAN_KINDS:
            raise ValueError("completed batch span must be graph_node or tool")
        _require_text(self.name, "name")
        _require_utc(self.started_at, "started_at")
        _require_utc(self.ended_at, "ended_at")
        if self.ended_at < self.started_at:
            raise ValueError("span ended_at must not precede started_at")
        _require_non_negative(self.duration_us, "duration_us")
        if self.status not in _FINAL_STATUSES:
            raise ValueError("span status must be succeeded or failed")
        if self.status == "succeeded" and self.error_type is not None:
            raise ValueError("succeeded span cannot have error_type")
        if self.error_type is not None:
            _require_identifier(self.error_type, "error_type")
        if self.span_kind == "graph_node":
            if self.name not in _GRAPH_NODE_NAMES:
                raise ValueError("graph span name must be a registered node constant")
            if any(
                value is not None
                for value in (self.parent_span_id, self.source_round_index, self.tool_effect)
            ):
                raise ValueError("graph span cannot have tool/parent fields")
        else:
            if _SAFE_TOOL_NAME.fullmatch(self.name) is None:
                raise ValueError("tool span name must be a registered safe name")
            if self.parent_span_id is None:
                raise ValueError("tool span requires parent_span_id")
            _require_uuid4(self.parent_span_id, "parent_span_id")
            if self.source_round_index is None:
                raise ValueError("tool span requires source_round_index")
            _require_positive(self.source_round_index, "source_round_index")
            if self.tool_effect is not None and self.tool_effect not in _TOOL_EFFECTS:
                raise ValueError("invalid tool_effect")
            if self.error_type is not None and self.error_type not in _TOOL_ERROR_TYPES:
                raise ValueError("tool error_type must be a fixed telemetry category")


@dataclass(frozen=True)
class CompletedRun:
    run_id: str
    ended_at: datetime
    duration_us: int
    status: FinalStatus
    spans: tuple[CompletedSpan, ...] = ()
    error_type: str | None = None
    tick_id: int | None = None

    def __post_init__(self) -> None:
        _require_uuid4(self.run_id, "run_id")
        _require_utc(self.ended_at, "ended_at")
        _require_non_negative(self.duration_us, "duration_us")
        if self.status not in _FINAL_STATUSES:
            raise ValueError("run status must be succeeded or failed")
        if self.status == "succeeded" and self.error_type is not None:
            raise ValueError("succeeded run cannot have error_type")
        if self.error_type is not None:
            _require_identifier(self.error_type, "error_type")
        if self.tick_id is not None:
            _require_positive(self.tick_id, "tick_id")
            if self.status != "succeeded":
                raise ValueError("failed run cannot have tick_id")
        if not isinstance(self.spans, tuple):
            raise TypeError("spans must be a tuple")
        if any(not isinstance(span, CompletedSpan) for span in self.spans):
            raise TypeError("spans must contain CompletedSpan values")
        if any(span.run_id != self.run_id for span in self.spans):
            raise ValueError("all completed spans must belong to the run")
        span_ids = {span.span_id for span in self.spans}
        sequences = {span.sequence for span in self.spans}
        if len(span_ids) != len(self.spans) or len(sequences) != len(self.spans):
            raise ValueError("completed spans must have unique ids and sequences")


class TelemetrySink(Protocol):
    """Narrow write protocol; implementations must not receive arbitrary mappings."""

    def start_run(self, run: RunStart) -> None: ...

    def start_llm_request(self, request: LlmRequestStart) -> None: ...

    def finish_llm_request(self, request: LlmRequestFinish) -> None: ...

    def finish_run(self, run: CompletedRun) -> None: ...

    def close(self) -> None: ...


class TelemetryRecorder(Protocol):
    """Run-local, fail-open facade surface used by instrumentation hooks."""

    def capture(
        self,
        run_id: str,
        operation: str,
        action: Callable[[], _CaptureT],
    ) -> _CaptureT | None: ...

    def start_run_from(self, run_id: str, factory: Callable[[], RunStart]) -> None: ...

    def start_llm_request_from(
        self,
        run_id: str,
        factory: Callable[[], LlmRequestStart],
    ) -> None: ...

    def finish_llm_request_from(
        self,
        run_id: str,
        factory: Callable[[], LlmRequestFinish],
    ) -> None: ...

    def finish_run_from(
        self,
        run_id: str,
        factory: Callable[[], CompletedRun],
    ) -> None: ...
