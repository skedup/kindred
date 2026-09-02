"""Disabled telemetry sink."""

from __future__ import annotations

from kindred.telemetry.contracts import (
    CompletedRun,
    LlmRequestFinish,
    LlmRequestStart,
    RunStart,
)


class NoopTelemetrySink:
    """A structurally valid sink with no side effects."""

    def start_run(self, run: RunStart) -> None:
        del run

    def start_llm_request(self, request: LlmRequestStart) -> None:
        del request

    def finish_llm_request(self, request: LlmRequestFinish) -> None:
        del request

    def finish_run(self, run: CompletedRun) -> None:
        del run

    def close(self) -> None:
        return
