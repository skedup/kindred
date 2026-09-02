"""Shared telemetry lifecycle around real tick and dream graph invocations."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import date
from typing import TYPE_CHECKING, TypeVar

from kindred import __version__
from kindred.telemetry import (
    CompletedRun,
    RunStart,
    TelemetryFacade,
    TelemetryLayout,
    create_telemetry_facade,
    new_trace_id,
    run_context,
    utc_now,
)
from kindred.telemetry.contracts import ExecutionMode, FinalStatus, RunKind, TriggerSource

if TYPE_CHECKING:
    from kindred.config import KindredConfig
    from kindred.telemetry import RunBuffer

_ResultT = TypeVar("_ResultT")


def create_runtime_telemetry(config: KindredConfig) -> TelemetryFacade:
    """Create the process-local facade from the fixed life-root layout."""

    return create_telemetry_facade(
        enabled=config.observability.enabled,
        db_path=TelemetryLayout.from_life_root(config.paths.life_root).db_path,
        retention_days=config.observability.retention_days,
    )


def observed_invoke(
    invoke: Callable[[], _ResultT],
    *,
    telemetry: TelemetryFacade | None,
    run_kind: RunKind,
    execution_mode: ExecutionMode,
    trigger_source: TriggerSource | None = None,
    dream_date: date | None = None,
) -> _ResultT:
    """Invoke business code unchanged while recording one fail-open run lifecycle."""

    if telemetry is None or not telemetry.is_recording:
        return invoke()

    run_id = new_trace_id()
    started_at = utc_now()
    started_ns = time.perf_counter_ns()
    with run_context(run_id, telemetry=telemetry) as run:
        telemetry.start_run_from(
            run_id,
            lambda: RunStart(
                run_id=run_id,
                run_kind=run_kind,
                execution_mode=execution_mode,
                trigger_source=trigger_source,
                dream_date=dream_date,
                started_at=started_at,
                app_version=__version__,
            ),
        )
        try:
            result = invoke()
        except BaseException as exc:
            _finish_run(
                telemetry,
                run,
                started_ns=started_ns,
                status="failed",
                error_type=type(exc).__name__,
            )
            raise
        _finish_run(
            telemetry,
            run,
            started_ns=started_ns,
            status="succeeded",
            tick_id=_tick_id(result) if run_kind == "tick" else None,
        )
        return result


def _finish_run(
    telemetry: TelemetryFacade,
    run: RunBuffer,
    *,
    started_ns: int,
    status: FinalStatus,
    error_type: str | None = None,
    tick_id: int | None = None,
) -> None:
    ended_at = utc_now()
    duration_us = max(0, (time.perf_counter_ns() - started_ns) // 1_000)
    telemetry.finish_run_from(
        run.run_id,
        lambda: CompletedRun(
            run_id=run.run_id,
            ended_at=ended_at,
            duration_us=duration_us,
            status=status,
            spans=run.completed_spans(),
            error_type=error_type,
            tick_id=tick_id,
        ),
    )


def _tick_id(result: object) -> int | None:
    if not isinstance(result, Mapping):
        return None
    value = result.get("tick_id")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


__all__ = ["create_runtime_telemetry", "observed_invoke"]
