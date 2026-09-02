"""Local, content-free telemetry kernel."""

from importlib import import_module
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from kindred.telemetry.context import (
        LlmRequestObservation,
        RunBuffer,
        RunContext,
        current_run,
        current_span_id,
        current_stage,
        next_sequence,
        observe_graph_node,
        observe_llm_request,
        observe_tool_call,
        record_unknown_tool,
        run_context,
        span_context,
        stage_context,
    )
    from kindred.telemetry.contracts import (
        CanonicalUsage,
        CompletedRun,
        CompletedSpan,
        LlmRequestFinish,
        LlmRequestStart,
        LlmRole,
        PromptShape,
        RunStart,
        StageStart,
        TelemetryRecorder,
        TelemetrySink,
        new_trace_id,
        utc_now,
    )
    from kindred.telemetry.facade import TelemetryFacade, create_telemetry_facade
    from kindred.telemetry.layout import TelemetryLayout
    from kindred.telemetry.noop import NoopTelemetrySink
    from kindred.telemetry.query import (
        STALE_AFTER,
        InvalidTelemetryCursor,
        TelemetryQueryService,
        TelemetryQueryUnavailable,
        is_stale_run,
    )
    from kindred.telemetry.query_contracts import (
        ActMetrics,
        ActRoundBucket,
        LatencyPercentiles,
        StageLatency,
        TelemetryBudgetStatus,
        TelemetryRunDetailResponse,
        TelemetryRunItem,
        TelemetryRunListResponse,
        TelemetryRunStatus,
        TelemetrySpanDetail,
        TelemetryStageTree,
        TelemetrySummaryResponse,
        TelemetryUsageDetail,
        TelemetryWindow,
        TokenTotals,
        UsageBreakdown,
        UsageCoverage,
    )
    from kindred.telemetry.schema import CURRENT_TELEMETRY_SCHEMA_VERSION
    from kindred.telemetry.sqlite_sink import (
        SqliteTelemetrySink,
        TelemetrySchemaError,
        TelemetryWriteError,
    )
    from kindred.telemetry.usage import (
        extract_anthropic_usage,
        extract_claude_code_usage,
        extract_deepseek_usage,
        extract_gemini_usage,
        extract_openai_usage,
        safe_response_model,
    )

__all__ = [
    "ActMetrics",
    "ActRoundBucket",
    "CURRENT_TELEMETRY_SCHEMA_VERSION",
    "CanonicalUsage",
    "CompletedRun",
    "CompletedSpan",
    "LlmRequestFinish",
    "LlmRequestObservation",
    "LlmRequestStart",
    "LlmRole",
    "LatencyPercentiles",
    "NoopTelemetrySink",
    "PromptShape",
    "RunBuffer",
    "RunContext",
    "RunStart",
    "STALE_AFTER",
    "StageLatency",
    "SqliteTelemetrySink",
    "StageStart",
    "TelemetryFacade",
    "TelemetryLayout",
    "TelemetryBudgetStatus",
    "TelemetryQueryService",
    "TelemetryQueryUnavailable",
    "TelemetryRecorder",
    "TelemetryRunDetailResponse",
    "TelemetryRunItem",
    "TelemetryRunListResponse",
    "TelemetryRunStatus",
    "TelemetrySpanDetail",
    "TelemetryStageTree",
    "TelemetrySummaryResponse",
    "TelemetryUsageDetail",
    "TelemetryWindow",
    "TelemetrySchemaError",
    "TelemetrySink",
    "TelemetryWriteError",
    "TokenTotals",
    "UsageBreakdown",
    "UsageCoverage",
    "create_telemetry_facade",
    "current_run",
    "current_span_id",
    "current_stage",
    "is_stale_run",
    "InvalidTelemetryCursor",
    "extract_anthropic_usage",
    "extract_claude_code_usage",
    "extract_deepseek_usage",
    "extract_gemini_usage",
    "extract_openai_usage",
    "new_trace_id",
    "next_sequence",
    "observe_graph_node",
    "observe_llm_request",
    "observe_tool_call",
    "record_unknown_tool",
    "run_context",
    "span_context",
    "stage_context",
    "safe_response_model",
    "utc_now",
]

_EXPORT_MODULES: Final[dict[str, str]] = {
    **{
        name: "kindred.telemetry.context"
        for name in (
            "LlmRequestObservation",
            "RunBuffer",
            "RunContext",
            "current_run",
            "current_span_id",
            "current_stage",
            "next_sequence",
            "observe_graph_node",
            "observe_llm_request",
            "observe_tool_call",
            "record_unknown_tool",
            "run_context",
            "span_context",
            "stage_context",
        )
    },
    **{
        name: "kindred.telemetry.contracts"
        for name in (
            "CanonicalUsage",
            "CompletedRun",
            "CompletedSpan",
            "LlmRequestFinish",
            "LlmRequestStart",
            "LlmRole",
            "PromptShape",
            "RunStart",
            "StageStart",
            "TelemetryRecorder",
            "TelemetrySink",
            "new_trace_id",
            "utc_now",
        )
    },
    "TelemetryFacade": "kindred.telemetry.facade",
    "create_telemetry_facade": "kindred.telemetry.facade",
    "TelemetryLayout": "kindred.telemetry.layout",
    "NoopTelemetrySink": "kindred.telemetry.noop",
    **{
        name: "kindred.telemetry.query"
        for name in (
            "STALE_AFTER",
            "InvalidTelemetryCursor",
            "TelemetryQueryService",
            "TelemetryQueryUnavailable",
            "is_stale_run",
        )
    },
    **{
        name: "kindred.telemetry.query_contracts"
        for name in (
            "ActMetrics",
            "ActRoundBucket",
            "LatencyPercentiles",
            "StageLatency",
            "TelemetryBudgetStatus",
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
        )
    },
    "CURRENT_TELEMETRY_SCHEMA_VERSION": "kindred.telemetry.schema",
    **{
        name: "kindred.telemetry.sqlite_sink"
        for name in ("SqliteTelemetrySink", "TelemetrySchemaError", "TelemetryWriteError")
    },
    **{
        name: "kindred.telemetry.usage"
        for name in (
            "extract_anthropic_usage",
            "extract_claude_code_usage",
            "extract_deepseek_usage",
            "extract_gemini_usage",
            "extract_openai_usage",
            "safe_response_model",
        )
    },
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
