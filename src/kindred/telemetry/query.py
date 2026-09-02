"""Bounded, read-only projections over the content-free telemetry database."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import math
import os
import sqlite3
import stat
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Literal, cast
from urllib.parse import quote

from pydantic import ValidationError

from kindred.telemetry.contracts import validate_trace_id
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
from kindred.telemetry.schema import (
    CURRENT_TELEMETRY_SCHEMA_VERSION,
    TELEMETRY_GRAPH_NODE_NAMES,
)

STALE_AFTER: Final = timedelta(hours=24)
QUERY_BUSY_TIMEOUT_MS: Final = 250
MAX_PERCENTILE_SAMPLES: Final = 50_000
MAX_BREAKDOWN_GROUPS: Final = 100
_CURSOR_MAX_LENGTH: Final = 256
_CURSOR_SIGNATURE_BYTES: Final = 16
_WINDOWS: Final[dict[TelemetryWindow, timedelta]] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
_USAGE_COLUMNS: Final[tuple[str, ...]] = (
    "usage_source",
    "reconciliation_status",
    "base_input_tokens",
    "visible_output_tokens",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "total_derived",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "reasoning_output_tokens",
    "tool_use_prompt_tokens",
    "unattributed_tokens",
    "provider_cost_microusd",
    "payload_json_bytes",
    "system_text_chars",
    "initial_user_text_chars",
    "tool_schema_json_chars",
    "response_schema_json_chars",
    "model_history_json_chars",
    "tool_result_json_chars",
)


class TelemetryQueryUnavailable(RuntimeError):
    """The local telemetry store cannot be safely queried."""

    def __init__(self) -> None:
        super().__init__("telemetry data is unavailable")


class InvalidTelemetryCursor(ValueError):
    """A pagination cursor is malformed, non-canonical, or unauthenticated."""

    def __init__(self) -> None:
        super().__init__("invalid telemetry cursor")


def is_stale_run(*, status: str, started_at: datetime, now: datetime) -> bool:
    """Derive stale display state without mutating another process's run."""

    if status != "running":
        return False
    if started_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("stale timestamps must be timezone-aware")
    return now - started_at >= STALE_AFTER


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _stored_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a stored timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC-aware")
    return parsed


def _stored_optional_datetime(value: object, field: str) -> datetime | None:
    return None if value is None else _stored_datetime(value, field)


def _percentiles(values: Sequence[int]) -> LatencyPercentiles:
    if not values:
        return LatencyPercentiles()
    ordered = sorted(values)

    def nearest_rank(percentile: float) -> int:
        return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]

    return LatencyPercentiles(
        samples=len(ordered),
        p50_us=nearest_rank(0.50),
        p95_us=nearest_rank(0.95),
        p99_us=nearest_rank(0.99),
    )


def _proportional_sample_limits(counts: dict[str, int], limit: int) -> dict[str, int]:
    """Allocate a bounded combined sample without changing the stage distribution."""

    total = sum(counts.values())
    if total <= limit:
        return counts.copy()
    allocations: dict[str, int] = {}
    remainders: list[tuple[int, str]] = []
    allocated = 0
    for name, count in sorted(counts.items()):
        allocation, remainder = divmod(count * limit, total)
        allocations[name] = allocation
        remainders.append((remainder, name))
        allocated += allocation
    for _, name in sorted(remainders, key=lambda item: (-item[0], item[1]))[: limit - allocated]:
        allocations[name] += 1
    return allocations


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return min(1.0, max(0.0, numerator / denominator))


def _regular_metadata(path: Path) -> os.stat_result | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TelemetryQueryUnavailable from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise TelemetryQueryUnavailable
    return metadata


def _file_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _immutable_fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


@dataclass(frozen=True)
class _UsageRollup:
    tokens: TokenTotals
    coverage: UsageCoverage
    breakdowns: list[UsageBreakdown]
    breakdowns_truncated: bool
    cache_read_ratio: float | None
    failed_spend_ratio: float | None
    act: ActMetrics


class TelemetryQueryService:
    """Per-request read service with no graph, provider, or writer dependency."""

    def __init__(
        self,
        path: Path,
        *,
        cursor_key: bytes,
        daily_token_warn: int | None = None,
        tick_duration_warn_seconds: float | None = None,
        dream_duration_warn_seconds: float | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(cursor_key, bytes) or len(cursor_key) < 16:
            raise ValueError("cursor_key must contain at least 16 bytes")
        if daily_token_warn is not None and (
            isinstance(daily_token_warn, bool)
            or not isinstance(daily_token_warn, int)
            or daily_token_warn <= 0
        ):
            raise ValueError("daily_token_warn must be a positive integer or None")
        for value, name in (
            (tick_duration_warn_seconds, "tick_duration_warn_seconds"),
            (dream_duration_warn_seconds, "dream_duration_warn_seconds"),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number or None")
        self._path = path
        self._cursor_key = cursor_key
        self._daily_token_warn = daily_token_warn
        self._tick_duration_warn_seconds = tick_duration_warn_seconds
        self._dream_duration_warn_seconds = dream_duration_warn_seconds
        self._now = now or (lambda: datetime.now(tz=timezone.utc))

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection | None]:
        metadata = _regular_metadata(self._path)
        if metadata is None:
            yield None
            return
        wal_path = Path(f"{self._path}-wal")
        shm_path = Path(f"{self._path}-shm")
        journal_path = Path(f"{self._path}-journal")
        wal_metadata = _regular_metadata(wal_path)
        shm_metadata = _regular_metadata(shm_path)
        if (wal_metadata is None) != (shm_metadata is None):
            raise TelemetryQueryUnavailable
        if _regular_metadata(journal_path) is not None:
            raise TelemetryQueryUnavailable
        immutable = wal_metadata is None
        immutable_fingerprint = _immutable_fingerprint(metadata)
        immutable_parameter = "&immutable=1" if immutable else ""
        uri = f"file:{quote(str(self._path), safe='/')}?mode=ro{immutable_parameter}"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                uri,
                uri=True,
                timeout=QUERY_BUSY_TIMEOUT_MS / 1000,
            )
            opened_metadata = _regular_metadata(self._path)
            if opened_metadata is None or _file_identity(opened_metadata) != _file_identity(
                metadata
            ):
                raise TelemetryQueryUnavailable
            opened_wal = _regular_metadata(wal_path)
            opened_shm = _regular_metadata(shm_path)
            if immutable:
                if opened_wal is not None or opened_shm is not None:
                    raise TelemetryQueryUnavailable
            elif (
                opened_wal is None
                or opened_shm is None
                or wal_metadata is None
                or shm_metadata is None
                or _file_identity(opened_wal) != _file_identity(wal_metadata)
                or _file_identity(opened_shm) != _file_identity(shm_metadata)
            ):
                raise TelemetryQueryUnavailable
            if _regular_metadata(journal_path) is not None:
                raise TelemetryQueryUnavailable
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            conn.execute(f"PRAGMA busy_timeout = {QUERY_BUSY_TIMEOUT_MS}")
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT MAX(version) AS version FROM telemetry_schema_version"
            ).fetchone()
            if row is None or row["version"] != CURRENT_TELEMETRY_SCHEMA_VERSION:
                raise TelemetryQueryUnavailable
        except TelemetryQueryUnavailable:
            if conn is not None:
                conn.close()
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            if conn is not None:
                conn.close()
            raise TelemetryQueryUnavailable from exc
        try:
            yield conn
            if immutable:
                final_metadata = _regular_metadata(self._path)
                if (
                    final_metadata is None
                    or _immutable_fingerprint(final_metadata) != immutable_fingerprint
                    or _regular_metadata(wal_path) is not None
                    or _regular_metadata(shm_path) is not None
                    or _regular_metadata(journal_path) is not None
                ):
                    raise TelemetryQueryUnavailable
        finally:
            conn.close()

    def _current_time(self) -> datetime:
        now = self._now()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise TelemetryQueryUnavailable
        return now.astimezone(timezone.utc)

    def _encode_cursor(self, started_at: datetime, run_id: str) -> str:
        payload = f"v1|{_iso(started_at)}|{run_id}".encode()
        signature = hmac.new(self._cursor_key, payload, hashlib.sha256).digest()[
            :_CURSOR_SIGNATURE_BYTES
        ]
        return base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode("ascii")

    def _decode_cursor(self, cursor: str) -> tuple[datetime, str]:
        if not isinstance(cursor, str) or not cursor or len(cursor) > _CURSOR_MAX_LENGTH:
            raise InvalidTelemetryCursor
        try:
            raw = base64.b64decode(
                cursor + "=" * (-len(cursor) % 4),
                altchars=b"-_",
                validate=True,
            )
            payload = raw[:-_CURSOR_SIGNATURE_BYTES]
            signature = raw[-_CURSOR_SIGNATURE_BYTES:]
            expected = hmac.new(self._cursor_key, payload, hashlib.sha256).digest()[
                :_CURSOR_SIGNATURE_BYTES
            ]
            if len(signature) != _CURSOR_SIGNATURE_BYTES or not hmac.compare_digest(
                signature, expected
            ):
                raise InvalidTelemetryCursor
            canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
            if canonical != cursor:
                raise InvalidTelemetryCursor
            version, timestamp, run_id = payload.decode("ascii").split("|")
            if version != "v1":
                raise InvalidTelemetryCursor
            started_at = _stored_datetime(timestamp, "cursor timestamp")
            validate_trace_id(run_id, "run_id")
        except (UnicodeError, binascii.Error, TypeError, ValueError) as exc:
            if isinstance(exc, InvalidTelemetryCursor):
                raise
            raise InvalidTelemetryCursor from exc
        return started_at, run_id

    def summary(self, window: TelemetryWindow) -> TelemetrySummaryResponse:
        if window not in _WINDOWS:
            raise ValueError("unsupported telemetry window")
        now = self._current_time()
        from_at = now - _WINDOWS[window]
        try:
            with self._connection() as conn:
                if conn is None:
                    return self._empty_summary(window, from_at, now)
                return self._summary_from(conn, window, from_at, now)
        except TelemetryQueryUnavailable:
            raise
        except (sqlite3.Error, TypeError, ValueError, ValidationError) as exc:
            raise TelemetryQueryUnavailable from exc

    def _empty_summary(
        self, window: TelemetryWindow, from_at: datetime, now: datetime
    ) -> TelemetrySummaryResponse:
        return TelemetrySummaryResponse(
            window=window,
            from_at=from_at,
            to_at=now,
            empty=True,
            run_count=0,
            succeeded_run_count=0,
            failed_run_count=0,
            running_run_count=0,
            stale_run_count=0,
            tokens=TokenTotals(),
            coverage=UsageCoverage(),
            run_latency=LatencyPercentiles(),
            stage_latency=LatencyPercentiles(),
            budget=self._budget(window, TokenTotals(), {}, None),
        )

    def _summary_from(
        self,
        conn: sqlite3.Connection,
        window: TelemetryWindow,
        from_at: datetime,
        now: datetime,
    ) -> TelemetrySummaryResponse:
        from_iso = _iso(from_at)
        to_iso = _iso(now)
        stale_iso = _iso(now - STALE_AFTER)
        run_counts = conn.execute(
            """
            SELECT COUNT(*) AS run_count,
                   SUM(status = 'succeeded') AS succeeded_count,
                   SUM(status = 'failed') AS failed_count,
                   SUM(status = 'running' AND started_at > ?) AS running_count,
                   SUM(status = 'running' AND started_at <= ?) AS stale_count
            FROM run INDEXED BY idx_run_started
            WHERE execution_mode = 'real' AND started_at >= ? AND started_at <= ?
            """,
            (stale_iso, stale_iso, from_iso, to_iso),
        ).fetchone()
        usage = self._usage_rollup(conn, from_iso, to_iso)

        tuple_cursor = conn.cursor()
        tuple_cursor.row_factory = None
        run_rows = tuple_cursor.execute(
            """
            SELECT run_kind, duration_us FROM run INDEXED BY idx_run_started
            WHERE execution_mode = 'real' AND started_at >= ? AND started_at <= ?
              AND duration_us IS NOT NULL
            ORDER BY started_at DESC LIMIT ?
            """,
            (from_iso, to_iso, MAX_PERCENTILE_SAMPLES),
        ).fetchall()
        run_latencies = [duration_us for _, duration_us in run_rows]
        run_by_kind: dict[str, list[int]] = defaultdict(list)
        for run_kind, duration_us in run_rows:
            run_by_kind[run_kind].append(duration_us)

        per_stage_limit = max(1, MAX_PERCENTILE_SAMPLES // len(TELEMETRY_GRAPH_NODE_NAMES))
        stored_stage_counts = {
            name: duration_count
            for name, duration_count in tuple_cursor.execute(
                """
                SELECT s.name, COUNT(s.duration_us)
                FROM span s INDEXED BY idx_span_kind_name_started
                JOIN run r ON r.run_id = s.run_id
                WHERE s.span_kind = 'graph_node' AND s.started_at >= ?
                  AND r.execution_mode = 'real'
                  AND r.started_at >= ? AND r.started_at <= ?
                GROUP BY s.name
                """,
                (from_iso, from_iso, to_iso),
            ).fetchall()
        }
        if not stored_stage_counts.keys() <= TELEMETRY_GRAPH_NODE_NAMES:
            raise ValueError("stored graph stage name is not registered")
        combined_limits = _proportional_sample_limits(stored_stage_counts, MAX_PERCENTILE_SAMPLES)
        stage_samples: list[int] = []
        stage_by_name: dict[str, list[int]] = defaultdict(list)
        for stage_name, duration_count in sorted(stored_stage_counts.items()):
            detail_limit = min(duration_count, per_stage_limit)
            combined_limit = combined_limits[stage_name]
            sample_limit = max(detail_limit, combined_limit)
            if sample_limit == 0:
                continue
            samples = [
                duration_us
                for (duration_us,) in tuple_cursor.execute(
                    """
                    SELECT s.duration_us
                    FROM span s INDEXED BY idx_span_kind_name_started
                    JOIN run r ON r.run_id = s.run_id
                    WHERE s.span_kind = 'graph_node' AND s.name = ?
                      AND s.started_at >= ? AND s.duration_us IS NOT NULL
                      AND r.execution_mode = 'real'
                      AND r.started_at >= ? AND r.started_at <= ?
                    ORDER BY s.started_at DESC LIMIT ?
                    """,
                    (stage_name, from_iso, from_iso, to_iso, sample_limit),
                ).fetchall()
            ]
            stage_by_name[stage_name].extend(samples[:detail_limit])
            stage_samples.extend(samples[:combined_limit])

        kind_p95 = {
            "tick": _percentiles(run_by_kind.get("tick", [])).p95_us,
            "dream": _percentiles(run_by_kind.get("dream", [])).p95_us,
        }
        return TelemetrySummaryResponse(
            window=window,
            from_at=from_at,
            to_at=now,
            empty=run_counts["run_count"] == 0,
            run_count=run_counts["run_count"],
            succeeded_run_count=run_counts["succeeded_count"] or 0,
            failed_run_count=run_counts["failed_count"] or 0,
            running_run_count=run_counts["running_count"] or 0,
            stale_run_count=run_counts["stale_count"] or 0,
            tokens=usage.tokens,
            coverage=usage.coverage,
            run_latency=_percentiles(run_latencies),
            stage_latency=_percentiles(stage_samples),
            stage_latencies=[
                StageLatency(name=name, latency=_percentiles(values))
                for name, values in sorted(stage_by_name.items())
            ],
            breakdowns=usage.breakdowns,
            breakdowns_truncated=usage.breakdowns_truncated,
            cache_read_ratio=usage.cache_read_ratio,
            failed_spend_ratio=usage.failed_spend_ratio,
            act=usage.act,
            budget=self._budget(
                window,
                usage.tokens,
                kind_p95,
                total_complete=(
                    usage.coverage.request_count > 0
                    and usage.coverage.total_available == usage.coverage.request_count
                ),
            ),
        )

    def _usage_rollup(self, conn: sqlite3.Connection, from_iso: str, to_iso: str) -> _UsageRollup:
        tuple_cursor = conn.cursor()
        tuple_cursor.row_factory = None
        rows = tuple_cursor.execute(
            """
            SELECT s.run_id, r.status AS run_status, s.llm_role, s.round_index, s.provider,
                   COALESCE(s.response_model, s.requested_model) AS model,
                   u.input_tokens, u.output_tokens, u.total_tokens,
                   u.cache_read_input_tokens
            FROM span s INDEXED BY uq_span_llm_round
            JOIN run r ON r.run_id = s.run_id
            LEFT JOIN llm_usage u ON u.span_id = s.span_id
            WHERE s.span_kind = 'llm_request'
              AND r.execution_mode = 'real'
              AND r.started_at >= ? AND r.started_at <= ?
            ORDER BY s.run_id, s.llm_role, s.round_index
            """,
            (from_iso, to_iso),
        )
        request_count = 0
        total_available = 0
        breakdown_available = 0
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        cache_tokens = 0
        cache_input_tokens = 0
        failed_tokens = 0
        grouped: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
        distribution: Counter[int] = Counter()
        amplification_total = 0.0
        amplification_count = 0
        current_act_run: str | None = None
        current_max_round = 0
        current_first_input: int | None = None
        current_last_input: int | None = None
        for (
            run_id,
            run_status,
            llm_role,
            round_index,
            provider,
            model,
            row_input,
            row_output,
            row_total,
            cache_read,
        ) in rows:
            request_count += 1
            if row_total is not None:
                total_available += 1
                total_tokens += row_total
                if run_status == "failed":
                    failed_tokens += row_total
            if row_input is not None and row_output is not None:
                breakdown_available += 1
            if row_input is not None:
                input_tokens += row_input
            if row_output is not None:
                output_tokens += row_output
            if row_input is not None and row_input > 0 and cache_read is not None:
                cache_tokens += cache_read
                cache_input_tokens += row_input
            for dimension, key in (
                ("role", llm_role),
                ("provider", provider),
                ("model", model),
            ):
                values = grouped[(dimension, key)]
                values[0] += 1
                values[1] += row_input or 0
                values[2] += row_output or 0
                values[3] += row_total or 0
            if llm_role == "act.llm":
                if current_act_run is not None and current_act_run != run_id:
                    distribution[current_max_round] += 1
                    if (
                        current_first_input is not None
                        and current_first_input > 0
                        and current_last_input is not None
                    ):
                        amplification_total += current_last_input / current_first_input
                        amplification_count += 1
                    current_max_round = 0
                    current_first_input = None
                    current_last_input = None
                current_act_run = run_id
                if round_index > current_max_round:
                    current_max_round = round_index
                    current_last_input = row_input
                if round_index == 1:
                    current_first_input = row_input

        breakdowns: list[UsageBreakdown] = []
        breakdowns_truncated = False
        for dimension in ("role", "provider", "model"):
            dimension_rows = sorted(
                (
                    (key, values)
                    for (candidate, key), values in grouped.items()
                    if candidate == dimension
                ),
                key=lambda item: (-item[1][3], item[0]),
            )
            if len(dimension_rows) > MAX_BREAKDOWN_GROUPS:
                breakdowns_truncated = True
            dimension_rows = dimension_rows[:MAX_BREAKDOWN_GROUPS]
            breakdowns.extend(
                UsageBreakdown(
                    dimension=cast(Literal["role", "provider", "model"], dimension),
                    key=key,
                    request_count=values[0],
                    tokens=TokenTotals(
                        input_tokens=values[1],
                        output_tokens=values[2],
                        total_tokens=values[3],
                    ),
                )
                for key, values in dimension_rows
            )

        if current_act_run is not None:
            distribution[current_max_round] += 1
            if (
                current_first_input is not None
                and current_first_input > 0
                and current_last_input is not None
            ):
                amplification_total += current_last_input / current_first_input
                amplification_count += 1
        tokens = TokenTotals(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
        return _UsageRollup(
            tokens=tokens,
            coverage=UsageCoverage(
                request_count=request_count,
                total_available=total_available,
                breakdown_available=breakdown_available,
                total_ratio=(total_available / request_count if request_count else 0.0),
                breakdown_ratio=(breakdown_available / request_count if request_count else 0.0),
            ),
            breakdowns=breakdowns,
            breakdowns_truncated=breakdowns_truncated,
            cache_read_ratio=_ratio(cache_tokens, cache_input_tokens),
            failed_spend_ratio=_ratio(failed_tokens, total_tokens),
            act=ActMetrics(
                round_distribution=[
                    ActRoundBucket(rounds=rounds, run_count=count)
                    for rounds, count in sorted(distribution.items())
                ],
                input_amplification_mean=(
                    amplification_total / amplification_count if amplification_count else None
                ),
            ),
        )

    def _budget(
        self,
        window: TelemetryWindow,
        tokens: TokenTotals,
        kind_p95: dict[str, int | None],
        total_complete: bool | None,
    ) -> TelemetryBudgetStatus:
        tick_p95 = kind_p95.get("tick")
        dream_p95 = kind_p95.get("dream")
        return TelemetryBudgetStatus(
            daily_token_warn=self._daily_token_warn,
            daily_token_exceeded=self._daily_token_exceeded(
                window=window,
                known_total=tokens.total_tokens,
                total_complete=total_complete,
            ),
            tick_duration_warn_seconds=self._tick_duration_warn_seconds,
            tick_duration_exceeded=(
                tick_p95 > self._tick_duration_warn_seconds * 1_000_000
                if tick_p95 is not None and self._tick_duration_warn_seconds is not None
                else None
            ),
            dream_duration_warn_seconds=self._dream_duration_warn_seconds,
            dream_duration_exceeded=(
                dream_p95 > self._dream_duration_warn_seconds * 1_000_000
                if dream_p95 is not None and self._dream_duration_warn_seconds is not None
                else None
            ),
        )

    def _daily_token_exceeded(
        self,
        *,
        window: TelemetryWindow,
        known_total: int,
        total_complete: bool | None,
    ) -> bool | None:
        threshold = self._daily_token_warn
        if window != "24h" or threshold is None:
            return None
        if known_total > threshold:
            return True
        if total_complete is not True:
            return None
        return False

    def list_runs(
        self,
        *,
        cursor: str | None = None,
        limit: int = 20,
        kind: Literal["tick", "dream"] | None = None,
        status: Literal["running", "succeeded", "failed", "stale"] | None = None,
        include_mock: bool = False,
    ) -> TelemetryRunListResponse:
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if kind not in (None, "tick", "dream"):
            raise ValueError("kind must be tick, dream, or None")
        if status not in (None, "running", "succeeded", "failed", "stale"):
            raise ValueError("invalid run status")
        if type(include_mock) is not bool:
            raise TypeError("include_mock must be a bool")
        cursor_value = self._decode_cursor(cursor) if cursor is not None else None
        now = self._current_time()
        try:
            with self._connection() as conn:
                if conn is None:
                    return TelemetryRunListResponse(empty=True)
                where = ["1 = 1"]
                parameters: list[object] = []
                if not include_mock:
                    where.append("execution_mode = 'real'")
                if kind is not None:
                    where.append("run_kind = ?")
                    parameters.append(kind)
                stale_iso = _iso(now - STALE_AFTER)
                if status == "stale":
                    where.append("status = 'running' AND started_at <= ?")
                    parameters.append(stale_iso)
                elif status == "running":
                    where.append("status = 'running' AND started_at > ?")
                    parameters.append(stale_iso)
                elif status is not None:
                    where.append("status = ?")
                    parameters.append(status)
                if cursor_value is not None:
                    cursor_time, cursor_run_id = cursor_value
                    where.append("(started_at < ? OR (started_at = ? AND run_id < ?))")
                    parameters.extend((_iso(cursor_time), _iso(cursor_time), cursor_run_id))
                parameters.append(limit + 1)
                rows = conn.execute(
                    f"""
                    SELECT * FROM run WHERE {" AND ".join(where)}
                    ORDER BY started_at DESC, run_id DESC LIMIT ?
                    """,
                    parameters,
                ).fetchall()
                visible = rows[:limit]
                aggregates = self._run_aggregates(conn, [row["run_id"] for row in visible])
                items = [self._run_item(row, aggregates.get(row["run_id"]), now) for row in visible]
                next_cursor = (
                    self._encode_cursor(
                        _stored_datetime(visible[-1]["started_at"], "started_at"),
                        visible[-1]["run_id"],
                    )
                    if len(rows) > limit and visible
                    else None
                )
                return TelemetryRunListResponse(
                    empty=not items, items=items, next_cursor=next_cursor
                )
        except (InvalidTelemetryCursor, TelemetryQueryUnavailable):
            raise
        except (sqlite3.Error, TypeError, ValueError, ValidationError) as exc:
            raise TelemetryQueryUnavailable from exc

    def _run_aggregates(
        self, conn: sqlite3.Connection, run_ids: Sequence[str]
    ) -> dict[str, sqlite3.Row]:
        if not run_ids:
            return {}
        placeholders = ",".join("?" for _ in run_ids)
        rows = conn.execute(
            f"""
            SELECT s.run_id, COUNT(*) AS span_count,
                   SUM(s.span_kind = 'llm_request') AS request_count,
                   SUM(s.span_kind = 'llm_request' AND u.total_tokens IS NOT NULL)
                       AS total_available,
                   COALESCE(SUM(u.input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(u.output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(u.total_tokens), 0) AS total_tokens
            FROM span s LEFT JOIN llm_usage u ON u.span_id = s.span_id
            WHERE s.run_id IN ({placeholders}) GROUP BY s.run_id
            """,
            tuple(run_ids),
        ).fetchall()
        return {row["run_id"]: row for row in rows}

    def _run_item(
        self, row: sqlite3.Row, aggregate: sqlite3.Row | None, now: datetime
    ) -> TelemetryRunItem:
        started_at = _stored_datetime(row["started_at"], "started_at")
        stored_status = row["status"]
        status = (
            "stale"
            if is_stale_run(status=stored_status, started_at=started_at, now=now)
            else stored_status
        )
        return TelemetryRunItem(
            run_id=row["run_id"],
            run_kind=row["run_kind"],
            execution_mode=row["execution_mode"],
            trigger_source=row["trigger_source"],
            dream_date=date.fromisoformat(row["dream_date"])
            if row["dream_date"] is not None
            else None,
            tick_id=row["tick_id"],
            started_at=started_at,
            ended_at=_stored_optional_datetime(row["ended_at"], "ended_at"),
            duration_us=row["duration_us"],
            status=cast(TelemetryRunStatus, status),
            error_type=row["error_type"],
            app_version=row["app_version"],
            span_count=aggregate["span_count"] if aggregate is not None else 0,
            request_count=aggregate["request_count"] if aggregate is not None else 0,
            total_available=(aggregate["total_available"] if aggregate is not None else 0),
            tokens=TokenTotals(
                input_tokens=aggregate["input_tokens"] if aggregate is not None else 0,
                output_tokens=aggregate["output_tokens"] if aggregate is not None else 0,
                total_tokens=aggregate["total_tokens"] if aggregate is not None else 0,
            ),
        )

    def run_detail(self, run_id: str) -> TelemetryRunDetailResponse | None:
        validate_trace_id(run_id, "run_id")
        now = self._current_time()
        try:
            with self._connection() as conn:
                if conn is None:
                    return None
                run_row = conn.execute("SELECT * FROM run WHERE run_id = ?", (run_id,)).fetchone()
                if run_row is None:
                    return None
                aggregate = self._run_aggregates(conn, [run_id]).get(run_id)
                usage_select = ", ".join(f"u.{column}" for column in _USAGE_COLUMNS)
                rows = conn.execute(
                    f"""
                    SELECT s.*, {usage_select}
                    FROM span s LEFT JOIN llm_usage u ON u.span_id = s.span_id
                    WHERE s.run_id = ? ORDER BY s.sequence ASC
                    """,
                    (run_id,),
                ).fetchall()
                stages: dict[str, tuple[TelemetrySpanDetail, list[TelemetrySpanDetail]]] = {}
                stage_order: list[str] = []
                children: list[TelemetrySpanDetail] = []
                for row in rows:
                    span = self._span_detail(row)
                    if span.span_kind == "graph_node":
                        stages[span.span_id] = (span, [])
                        stage_order.append(span.span_id)
                    else:
                        children.append(span)
                for child in children:
                    parent = stages.get(child.parent_span_id or "")
                    if parent is None:
                        raise ValueError("telemetry span parent is unavailable")
                    parent[1].append(child)
                return TelemetryRunDetailResponse(
                    run=self._run_item(run_row, aggregate, now),
                    stages=[
                        TelemetryStageTree(stage=stages[span_id][0], children=stages[span_id][1])
                        for span_id in stage_order
                    ],
                )
        except TelemetryQueryUnavailable:
            raise
        except (sqlite3.Error, TypeError, ValueError, ValidationError) as exc:
            raise TelemetryQueryUnavailable from exc

    def _span_detail(self, row: sqlite3.Row) -> TelemetrySpanDetail:
        usage = None
        if row["span_kind"] == "llm_request" and row["usage_source"] is not None:
            usage = TelemetryUsageDetail.model_validate(
                {
                    column: bool(row[column]) if column == "total_derived" else row[column]
                    for column in _USAGE_COLUMNS
                }
            )
        return TelemetrySpanDetail(
            span_id=row["span_id"],
            parent_span_id=row["parent_span_id"],
            sequence=row["sequence"],
            span_kind=row["span_kind"],
            name=row["name"],
            llm_role=row["llm_role"],
            round_index=row["round_index"],
            source_round_index=row["source_round_index"],
            tool_effect=row["tool_effect"],
            provider=row["provider"],
            requested_model=row["requested_model"],
            response_model=row["response_model"],
            started_at=_stored_datetime(row["started_at"], "started_at"),
            ended_at=_stored_optional_datetime(row["ended_at"], "ended_at"),
            duration_us=row["duration_us"],
            status=row["status"],
            error_type=row["error_type"],
            http_status=row["http_status"],
            usage=usage,
        )


__all__ = [
    "InvalidTelemetryCursor",
    "MAX_PERCENTILE_SAMPLES",
    "QUERY_BUSY_TIMEOUT_MS",
    "STALE_AFTER",
    "TelemetryQueryService",
    "TelemetryQueryUnavailable",
    "is_stale_run",
]
