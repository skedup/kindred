"""Context-local run/span identity without polluting LangGraph state."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import wraps
from typing import TypeAlias, TypeVar

from kindred.telemetry.contracts import (
    CanonicalUsage,
    CompletedSpan,
    FinalStatus,
    LlmRequestFinish,
    LlmRequestStart,
    LlmRole,
    PromptShape,
    StageStart,
    TelemetryRecorder,
    new_trace_id,
    utc_now,
    validate_trace_id,
)
from kindred_capability_sdk import ToolDef, ToolEffect

_StateT = TypeVar("_StateT")
_NodeResultT = TypeVar("_NodeResultT")


@dataclass
class RunBuffer:
    """Thread-safe run-local recorder state carried by a ContextVar."""

    run_id: str
    telemetry: TelemetryRecorder | None = field(default=None, repr=False)
    _sequence: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _spans_by_sequence: dict[int, CompletedSpan] = field(default_factory=dict, repr=False)
    _span_ids: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        validate_trace_id(self.run_id, "run_id")

    def next_sequence(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence

    def record_completed_span(self, span: CompletedSpan) -> None:
        """Buffer a prebuilt span through the same fail-open recorder boundary."""

        self.record_completed_span_from(lambda: span)

    def record_completed_span_from(self, factory: Callable[[], CompletedSpan]) -> None:
        """Build and buffer one completed span without affecting business flow."""

        recorder = self.telemetry
        if recorder is None:
            return
        recorder.capture(
            self.run_id,
            "record_completed_span",
            lambda: self._record_completed_span(factory()),
        )

    def _record_completed_span(self, span: CompletedSpan) -> None:

        if not isinstance(span, CompletedSpan):
            raise TypeError("span must be a CompletedSpan")
        if span.run_id != self.run_id:
            raise ValueError("completed span must belong to the active run")
        with self._lock:
            if span.sequence in self._spans_by_sequence or span.span_id in self._span_ids:
                raise ValueError("completed span ids and sequences must be unique within a run")
            self._spans_by_sequence[span.sequence] = span
            self._span_ids.add(span.span_id)

    def completed_spans(self) -> tuple[CompletedSpan, ...]:
        """Return a stable sequence-ordered snapshot for ``CompletedRun``."""

        with self._lock:
            return tuple(self._spans_by_sequence[key] for key in sorted(self._spans_by_sequence))


RunContext: TypeAlias = RunBuffer


_current_run: ContextVar[RunBuffer | None] = ContextVar("kindred_telemetry_run", default=None)
_current_span_id: ContextVar[str | None] = ContextVar("kindred_telemetry_span_id", default=None)
_current_stage: ContextVar[StageStart | None] = ContextVar(
    "kindred_telemetry_stage",
    default=None,
)


def current_run() -> RunBuffer | None:
    return _current_run.get()


def current_span_id() -> str | None:
    return _current_span_id.get()


def current_stage() -> StageStart | None:
    return _current_stage.get()


def next_sequence() -> int | None:
    run = current_run()
    return None if run is None else run.next_sequence()


@contextmanager
def run_context(
    run_id: str | None = None,
    *,
    telemetry: TelemetryRecorder | None = None,
) -> Iterator[RunBuffer]:
    """Install a run context and always restore the previous context."""

    if current_run() is not None:
        raise RuntimeError("nested telemetry run contexts are not supported")
    context = RunBuffer(run_id=run_id or new_trace_id(), telemetry=telemetry)
    run_token = _current_run.set(context)
    span_token = _current_span_id.set(None)
    stage_token = _current_stage.set(None)
    try:
        yield context
    finally:
        _current_stage.reset(stage_token)
        _current_span_id.reset(span_token)
        _current_run.reset(run_token)


@contextmanager
def stage_context(stage: StageStart) -> Iterator[StageStart]:
    """Install the typed active graph stage used by Provider and tool hooks."""

    if not isinstance(stage, StageStart):
        raise TypeError("stage must be a StageStart")
    run = current_run()
    if run is None:
        yield stage
        return
    if stage.run_id != run.run_id:
        raise ValueError("stage must belong to the active run")
    stage_token = _current_stage.set(stage)
    span_token = _current_span_id.set(stage.span_id)
    try:
        yield stage
    finally:
        _current_span_id.reset(span_token)
        _current_stage.reset(stage_token)


@contextmanager
def span_context(span_id: str) -> Iterator[None]:
    """Set the current parent span for a stage/request/tool scope."""

    if current_run() is None:
        yield
        return
    validate_trace_id(span_id, "span_id")
    token = _current_span_id.set(span_id)
    try:
        yield
    finally:
        _current_span_id.reset(token)


def observe_graph_node(
    name: str,
    node: Callable[[_StateT], _NodeResultT],
) -> Callable[[_StateT], _NodeResultT]:
    """Wrap one graph node without inspecting or copying its business state."""

    @wraps(node)
    def observed(state: _StateT) -> _NodeResultT:
        run = current_run()
        if run is None or run.telemetry is None:
            return node(state)
        started_at = utc_now()
        started_ns = time.perf_counter_ns()
        span_id = new_trace_id()
        sequence = run.next_sequence()
        stage = run.telemetry.capture(
            run.run_id,
            "start_graph_node",
            lambda: StageStart(
                span_id=span_id,
                run_id=run.run_id,
                sequence=sequence,
                name=name,
                started_at=started_at,
            ),
        )
        if not isinstance(stage, StageStart):
            return node(state)

        started_ns = time.perf_counter_ns()
        failure: BaseException | None = None
        try:
            with stage_context(stage):
                return node(state)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            ended_at = utc_now()
            duration_us = max(0, (time.perf_counter_ns() - started_ns) // 1_000)
            run.record_completed_span_from(
                lambda: CompletedSpan(
                    span_id=span_id,
                    run_id=run.run_id,
                    sequence=sequence,
                    span_kind="graph_node",
                    name=name,
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_us=duration_us,
                    status="failed" if failure is not None else "succeeded",
                    error_type=type(failure).__name__ if failure is not None else None,
                )
            )

    return observed


class ToolCallObservation:
    """Fail-open tool span completed after the dispatcher normalizes its result."""

    def __init__(
        self,
        *,
        name: str,
        effect: ToolEffect | None,
        round_index: int,
    ) -> None:
        self._run = current_run()
        self._stage = current_stage()
        self._name = name
        self._effect = effect
        self._round_index = round_index
        self._span_id: str | None = None
        self._sequence: int | None = None
        self._started_at = utc_now()
        self._started_ns = time.perf_counter_ns()
        run = self._run
        stage = self._stage
        if run is None or stage is None or run.telemetry is None:
            return

        def prepare() -> tuple[str, int]:
            candidate = CompletedSpan(
                span_id=new_trace_id(),
                run_id=run.run_id,
                sequence=run.next_sequence(),
                span_kind="tool",
                name=name,
                started_at=self._started_at,
                ended_at=self._started_at,
                duration_us=0,
                status="succeeded",
                parent_span_id=stage.span_id,
                source_round_index=round_index,
                tool_effect=effect,
            )
            return candidate.span_id, candidate.sequence

        prepared = run.telemetry.capture(run.run_id, "start_tool", prepare)
        if prepared is not None:
            self._span_id, self._sequence = prepared
            self._started_ns = time.perf_counter_ns()

    def finish(self, *, error_type: str | None = None) -> None:
        """Buffer the final safe tool classification; never expose handler payloads."""

        run = self._run
        stage = self._stage
        span_id = self._span_id
        sequence = self._sequence
        if run is None or stage is None or span_id is None or sequence is None:
            return
        ended_at = utc_now()
        duration_us = max(0, (time.perf_counter_ns() - self._started_ns) // 1_000)
        run.record_completed_span_from(
            lambda: CompletedSpan(
                span_id=span_id,
                run_id=run.run_id,
                sequence=sequence,
                span_kind="tool",
                name=self._name,
                started_at=self._started_at,
                ended_at=ended_at,
                duration_us=duration_us,
                status="failed" if error_type is not None else "succeeded",
                parent_span_id=stage.span_id,
                source_round_index=self._round_index,
                tool_effect=self._effect,
                error_type=error_type,
            )
        )


def observe_tool_call(tool_def: ToolDef, round_index: int) -> ToolCallObservation:
    """Start a known-tool observation using only trusted ``ToolDef`` metadata."""

    if not isinstance(tool_def, ToolDef):
        return ToolCallObservation(name="unknown_tool", effect=None, round_index=round_index)
    return ToolCallObservation(
        name=tool_def.name,
        effect=tool_def.effect,
        round_index=round_index,
    )


def record_unknown_tool(round_index: int) -> None:
    """Record one rejected model-generated tool name without persisting that name."""

    ToolCallObservation(name="unknown_tool", effect=None, round_index=round_index).finish(
        error_type="UnknownTool"
    )


class LlmRequestObservation:
    """Fail-open request checkpoint whose lifetime excludes tool execution."""

    def __init__(
        self,
        *,
        role: LlmRole,
        round_index: int,
        provider: str,
        requested_model: str,
        prompt_shape_factory: Callable[[], PromptShape],
    ) -> None:
        self._run = current_run()
        self._stage = current_stage()
        self._recorder = self._run.telemetry if self._run is not None else None
        self._prompt_shape_factory = prompt_shape_factory
        self._response_projection_factory: Callable[[], tuple[CanonicalUsage, str | None]] = (
            lambda: (CanonicalUsage.unavailable(), None)
        )
        self._payload_json_bytes: int | None = None
        self._http_status: int | None = None
        self._span_id: str | None = None
        self._started_at = utc_now()
        self._started_ns = time.perf_counter_ns()
        if self._run is None or self._stage is None or self._recorder is None:
            return
        run = self._run
        stage = self._stage
        span_id = new_trace_id()
        sequence = run.next_sequence()
        self._recorder.start_llm_request_from(
            run.run_id,
            lambda: LlmRequestStart(
                span_id=span_id,
                run_id=run.run_id,
                parent_stage=stage,
                sequence=sequence,
                llm_role=role,
                round_index=round_index,
                provider=provider,
                requested_model=requested_model,
                started_at=self._started_at,
            ),
        )
        self._span_id = span_id
        self._started_ns = time.perf_counter_ns()

    def record_response(
        self,
        *,
        http_status: int | None,
        usage_factory: Callable[[], CanonicalUsage],
        response_model_factory: Callable[[], str | None] = lambda: None,
    ) -> None:
        """Attach allowlisted response extractors without evaluating raw wire data here."""

        self._http_status = http_status
        self._response_projection_factory = lambda: (
            usage_factory(),
            response_model_factory(),
        )

    def record_response_from(
        self,
        *,
        http_status: int | None,
        projection_factory: Callable[[], tuple[CanonicalUsage, str | None]],
    ) -> None:
        """Defer a shared response projection into the facade's fail-open boundary."""

        self._http_status = http_status
        self._response_projection_factory = projection_factory

    def record_http_status(self, http_status: int) -> None:
        """Record an error status without parsing an untrusted Provider body."""

        self._http_status = http_status

    def record_payload_json_bytes(self, value: int) -> None:
        """Reuse the transport-owned encoded request size for the final shape."""

        self._payload_json_bytes = value

    def __enter__(self) -> LlmRequestObservation:
        return self

    def mark_request_started(self) -> None:
        """Reset monotonic timing immediately before the actual I/O boundary."""

        self._started_ns = time.perf_counter_ns()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        del exc_type, traceback
        run = self._run
        span_id = self._span_id
        recorder = self._recorder
        if run is None or span_id is None or recorder is None:
            return False
        ended_at = utc_now()
        duration_us = max(0, (time.perf_counter_ns() - self._started_ns) // 1_000)
        status: FinalStatus = "failed" if exc is not None else "succeeded"
        error_type = type(exc).__name__ if exc is not None else None
        recorder.finish_llm_request_from(
            run.run_id,
            lambda: self._finish_request(
                span_id=span_id,
                run_id=run.run_id,
                ended_at=ended_at,
                duration_us=duration_us,
                status=status,
                error_type=error_type,
            ),
        )
        return False

    def _finish_request(
        self,
        *,
        span_id: str,
        run_id: str,
        ended_at: datetime,
        duration_us: int,
        status: FinalStatus,
        error_type: str | None,
    ) -> LlmRequestFinish:
        usage, response_model = self._response_projection_factory()
        return LlmRequestFinish(
            span_id=span_id,
            run_id=run_id,
            ended_at=ended_at,
            duration_us=duration_us,
            status=status,
            usage=usage,
            prompt_shape=self._prompt_shape(),
            response_model=response_model,
            error_type=error_type,
            http_status=self._http_status,
        )

    def _prompt_shape(self) -> PromptShape:
        shape = self._prompt_shape_factory()
        if self._payload_json_bytes is None:
            return shape
        return replace(shape, payload_json_bytes=self._payload_json_bytes)


def observe_llm_request(
    *,
    role: LlmRole,
    round_index: int,
    provider: str,
    requested_model: str,
    prompt_shape_factory: Callable[[], PromptShape],
) -> LlmRequestObservation:
    """Create a request observation; inactive graph paths transparently Noop."""

    return LlmRequestObservation(
        role=role,
        round_index=round_index,
        provider=provider,
        requested_model=requested_model,
        prompt_shape_factory=prompt_shape_factory,
    )
