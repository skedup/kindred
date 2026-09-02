"""Fail-open facade around the telemetry sink."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from kindred.telemetry.contracts import (
    CompletedRun,
    LlmRequestFinish,
    LlmRequestStart,
    RunStart,
    TelemetrySink,
)
from kindred.telemetry.noop import NoopTelemetrySink

_LOG = logging.getLogger(__name__)
_T = TypeVar("_T")


class TelemetryFacade:
    """Contain sink failures and disable only the affected run."""

    def __init__(
        self,
        sink: TelemetrySink,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        slow_write_seconds: float = 0.050,
        warning_interval_seconds: float = 3600.0,
    ) -> None:
        self._sink = sink
        self._monotonic = monotonic
        self._slow_write_seconds = slow_write_seconds
        self._warning_interval_seconds = warning_interval_seconds
        self._state_lock = threading.RLock()
        self._disabled_runs: set[str] = set()
        self._last_warning: dict[str, float] = {}

    def start_run(self, run: RunStart) -> None:
        self.start_run_from(run.run_id, lambda: run)

    def start_run_from(self, run_id: str, factory: Callable[[], RunStart]) -> None:
        """Build and write a root inside the fail-open boundary."""

        def write() -> None:
            run = factory()
            if not isinstance(run, RunStart) or run.run_id != run_id:
                raise ValueError("run factory must return the expected RunStart")
            self._sink.start_run(run)

        self.capture(run_id, "start_run", write)

    def start_llm_request(self, request: LlmRequestStart) -> None:
        self.start_llm_request_from(request.run_id, lambda: request)

    def start_llm_request_from(
        self,
        run_id: str,
        factory: Callable[[], LlmRequestStart],
    ) -> None:
        """Build Provider request metadata and checkpoint it fail-open."""

        def write() -> None:
            request = factory()
            if not isinstance(request, LlmRequestStart) or request.run_id != run_id:
                raise ValueError("request factory must return the expected LlmRequestStart")
            self._sink.start_llm_request(request)

        self.capture(run_id, "start_llm_request", write)

    def finish_llm_request(self, request: LlmRequestFinish) -> None:
        self.finish_llm_request_from(request.run_id, lambda: request)

    def finish_llm_request_from(
        self,
        run_id: str,
        factory: Callable[[], LlmRequestFinish],
    ) -> None:
        """Extract/build Provider usage and checkpoint it fail-open."""

        def write() -> None:
            request = factory()
            if not isinstance(request, LlmRequestFinish) or request.run_id != run_id:
                raise ValueError("request factory must return the expected LlmRequestFinish")
            self._sink.finish_llm_request(request)

        self.capture(run_id, "finish_llm_request", write)

    def finish_run(self, run: CompletedRun) -> None:
        self.finish_run_from(run.run_id, lambda: run)

    def finish_run_from(self, run_id: str, factory: Callable[[], CompletedRun]) -> None:
        """Build and write the final run batch inside the fail-open boundary."""

        def write() -> None:
            run = factory()
            if not isinstance(run, CompletedRun) or run.run_id != run_id:
                raise ValueError("run factory must return the expected CompletedRun")
            self._sink.finish_run(run)

        try:
            self.capture(run_id, "finish_run", write)
        finally:
            with self._state_lock:
                self._disabled_runs.discard(run_id)

    def close(self) -> None:
        try:
            self._sink.close()
        except Exception as exc:  # noqa: BLE001 - telemetry must never own business failure
            self._warn("close", type(exc).__name__)

    def is_run_disabled(self, run_id: str) -> bool:
        with self._state_lock:
            return run_id in self._disabled_runs

    @property
    def is_recording(self) -> bool:
        """Whether this facade owns a real sink rather than the disabled/degraded Noop."""

        return not isinstance(self._sink, NoopTelemetrySink)

    def capture(self, run_id: str, operation: str, action: Callable[[], _T]) -> _T | None:
        """Execute telemetry-only construction/work and contain every failure."""

        with self._state_lock:
            if run_id in self._disabled_runs:
                return None
        started = self._monotonic()
        try:
            result = action()
        except Exception as exc:  # noqa: BLE001 - fail-open is the facade contract
            with self._state_lock:
                self._disabled_runs.add(run_id)
            self._warn(operation, type(exc).__name__)
            return None
        elapsed = self._monotonic() - started
        if elapsed > self._slow_write_seconds:
            if operation != "start_run":
                with self._state_lock:
                    self._disabled_runs.add(run_id)
            self._warn(operation, "SlowTelemetryWrite")
        return result

    def _warn(self, operation: str, error_type: str) -> None:
        key = f"{operation}:{error_type}"
        now = self._monotonic()
        with self._state_lock:
            previous = self._last_warning.get(key)
            if previous is not None and now - previous < self._warning_interval_seconds:
                return
            self._last_warning[key] = now
        _LOG.warning(
            "telemetry degraded operation=%s error_type=%s",
            operation,
            error_type,
        )


def create_telemetry_facade(
    *,
    enabled: bool,
    db_path: Path,
    retention_days: int,
) -> TelemetryFacade:
    """Build an enabled facade or fail open to a Noop sink."""

    if not enabled:
        return TelemetryFacade(NoopTelemetrySink())
    try:
        from kindred.telemetry.sqlite_sink import SqliteTelemetrySink

        sink: TelemetrySink = SqliteTelemetrySink.open(
            db_path,
            retention_days=retention_days,
        )
    except Exception as exc:  # noqa: BLE001 - initialization must not block Heart startup
        _LOG.warning(
            "telemetry degraded operation=initialize error_type=%s",
            type(exc).__name__,
        )
        sink = NoopTelemetrySink()
    return TelemetryFacade(sink)
