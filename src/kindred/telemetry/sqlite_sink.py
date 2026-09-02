"""Independent SQLite sink with atomic schema v1 and durable LLM checkpoints."""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Final

from kindred.telemetry.contracts import (
    CanonicalUsage,
    CompletedRun,
    CompletedSpan,
    LlmRequestFinish,
    LlmRequestStart,
    PromptShape,
    RunStart,
)
from kindred.telemetry.schema import CURRENT_TELEMETRY_SCHEMA_VERSION

_LOG = logging.getLogger(__name__)

WRITER_BUSY_TIMEOUT_MS: Final[int] = 25
RETENTION_RETRY_INTERVAL: Final[timedelta] = timedelta(hours=1)
RETENTION_DELETE_BATCH_SIZE: Final[int] = 10
RETENTION_INITIAL_DELAY_SECONDS: Final[float] = 0.100
RETENTION_BATCH_PAUSE_SECONDS: Final[float] = 0.010
RETENTION_CLOSE_WAIT_SECONDS: Final[float] = 1.0


class TelemetrySchemaError(RuntimeError):
    """The telemetry database cannot be safely used by this code version."""


class TelemetryWriteError(RuntimeError):
    """A lifecycle checkpoint did not update exactly the expected row."""


_MIGRATION_V1: Final[tuple[str, ...]] = (
    """
    CREATE TABLE telemetry_schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE run (
        run_id TEXT PRIMARY KEY,
        run_kind TEXT NOT NULL CHECK (run_kind IN ('tick', 'dream')),
        execution_mode TEXT NOT NULL CHECK (execution_mode IN ('real', 'mock')),
        trigger_source TEXT,
        dream_date TEXT,
        tick_id INTEGER,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        duration_us INTEGER CHECK (duration_us IS NULL OR duration_us >= 0),
        status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
        error_type TEXT,
        app_version TEXT NOT NULL,
        CHECK (
            trigger_source IS NULL
            OR trigger_source IN ('cold_start', 'heartbeat', 'watcher')
        ),
        CHECK (
            (run_kind = 'tick' AND trigger_source IS NOT NULL AND dream_date IS NULL)
            OR
            (run_kind = 'dream' AND trigger_source IS NULL AND dream_date IS NOT NULL)
        ),
        CHECK (
            (status = 'running' AND ended_at IS NULL
                AND duration_us IS NULL AND error_type IS NULL)
            OR
            (status IN ('succeeded', 'failed') AND ended_at IS NOT NULL
                AND duration_us IS NOT NULL)
        ),
        CHECK (status = 'failed' OR error_type IS NULL),
        CHECK (ended_at IS NULL OR ended_at >= started_at),
        CHECK (run_kind = 'tick' OR tick_id IS NULL),
        CHECK (length(app_version) BETWEEN 1 AND 128)
    )
    """,
    """
    CREATE TABLE span (
        span_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        parent_span_id TEXT,
        sequence INTEGER NOT NULL,
        span_kind TEXT NOT NULL CHECK (
            span_kind IN ('graph_node', 'llm_request', 'tool')
        ),
        name TEXT NOT NULL,
        llm_role TEXT,
        round_index INTEGER,
        source_round_index INTEGER,
        tool_effect TEXT,
        provider TEXT,
        requested_model TEXT,
        response_model TEXT,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        duration_us INTEGER CHECK (duration_us IS NULL OR duration_us >= 0),
        status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
        error_type TEXT,
        http_status INTEGER,
        UNIQUE (run_id, sequence),
        UNIQUE (span_id, run_id),
        FOREIGN KEY (run_id) REFERENCES run(run_id) ON DELETE CASCADE,
        FOREIGN KEY (parent_span_id, run_id)
            REFERENCES span(span_id, run_id) ON DELETE CASCADE,
        CHECK (
            (status = 'running' AND ended_at IS NULL
                AND duration_us IS NULL AND error_type IS NULL)
            OR
            (status IN ('succeeded', 'failed') AND ended_at IS NOT NULL
                AND duration_us IS NOT NULL)
        ),
        CHECK (status = 'failed' OR error_type IS NULL),
        CHECK (ended_at IS NULL OR ended_at >= started_at),
        CHECK (length(name) BETWEEN 1 AND 128),
        CHECK (
            llm_role IS NULL OR llm_role IN (
                'sense.llm', 'act.llm', 'dream.summarize',
                'dream.reflect', 'dream.gate', 'dream.excerpt'
            )
        ),
        CHECK (
            tool_effect IS NULL OR tool_effect IN (
                'read_only', 'staged_state_event',
                'external_side_effect', 'artifact_write'
            )
        ),
        CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599),
        CHECK (
            (span_kind = 'graph_node'
                AND parent_span_id IS NULL
                AND llm_role IS NULL AND round_index IS NULL
                AND source_round_index IS NULL AND tool_effect IS NULL
                AND provider IS NULL AND requested_model IS NULL
                AND response_model IS NULL AND http_status IS NULL)
            OR
            (span_kind = 'llm_request'
                AND parent_span_id IS NOT NULL
                AND llm_role IS NOT NULL AND round_index >= 1
                AND source_round_index IS NULL AND tool_effect IS NULL
                AND provider IS NOT NULL AND requested_model IS NOT NULL)
            OR
            (span_kind = 'tool'
                AND parent_span_id IS NOT NULL
                AND llm_role IS NULL AND round_index IS NULL
                AND source_round_index >= 1
                AND provider IS NULL AND requested_model IS NULL
                AND response_model IS NULL AND http_status IS NULL)
        )
    )
    """,
    """
    CREATE TABLE llm_usage (
        span_id TEXT PRIMARY KEY REFERENCES span(span_id) ON DELETE CASCADE,
        usage_source TEXT NOT NULL CHECK (
            usage_source IN ('provider_complete', 'provider_partial', 'unavailable')
        ),
        reconciliation_status TEXT NOT NULL CHECK (
            reconciliation_status IN (
                'exact', 'provider_total_only', 'partial', 'mismatch', 'unavailable'
            )
        ),
        base_input_tokens INTEGER CHECK (
            base_input_tokens IS NULL OR base_input_tokens >= 0
        ),
        visible_output_tokens INTEGER CHECK (
            visible_output_tokens IS NULL OR visible_output_tokens >= 0
        ),
        input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
        output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
        total_tokens INTEGER CHECK (total_tokens IS NULL OR total_tokens >= 0),
        total_derived INTEGER NOT NULL CHECK (total_derived IN (0, 1)),
        cache_read_input_tokens INTEGER CHECK (
            cache_read_input_tokens IS NULL OR cache_read_input_tokens >= 0
        ),
        cache_write_input_tokens INTEGER CHECK (
            cache_write_input_tokens IS NULL OR cache_write_input_tokens >= 0
        ),
        reasoning_output_tokens INTEGER CHECK (
            reasoning_output_tokens IS NULL OR reasoning_output_tokens >= 0
        ),
        tool_use_prompt_tokens INTEGER CHECK (
            tool_use_prompt_tokens IS NULL OR tool_use_prompt_tokens >= 0
        ),
        unattributed_tokens INTEGER CHECK (
            unattributed_tokens IS NULL OR unattributed_tokens >= 0
        ),
        provider_cost_microusd INTEGER CHECK (
            provider_cost_microusd IS NULL OR provider_cost_microusd >= 0
        ),
        payload_json_bytes INTEGER CHECK (
            payload_json_bytes IS NULL OR payload_json_bytes >= 0
        ),
        system_text_chars INTEGER CHECK (
            system_text_chars IS NULL OR system_text_chars >= 0
        ),
        initial_user_text_chars INTEGER CHECK (
            initial_user_text_chars IS NULL OR initial_user_text_chars >= 0
        ),
        tool_schema_json_chars INTEGER CHECK (
            tool_schema_json_chars IS NULL OR tool_schema_json_chars >= 0
        ),
        response_schema_json_chars INTEGER CHECK (
            response_schema_json_chars IS NULL OR response_schema_json_chars >= 0
        ),
        model_history_json_chars INTEGER CHECK (
            model_history_json_chars IS NULL OR model_history_json_chars >= 0
        ),
        tool_result_json_chars INTEGER CHECK (
            tool_result_json_chars IS NULL OR tool_result_json_chars >= 0
        ),
        CHECK (usage_source != 'provider_complete' OR total_tokens IS NOT NULL),
        CHECK (
            usage_source != 'provider_complete'
            OR (
                input_tokens IS NOT NULL AND output_tokens IS NOT NULL
                AND total_tokens IS NOT NULL
            )
        ),
        CHECK (
            total_derived = 0
            OR (
                reconciliation_status = 'exact'
                AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL
                AND total_tokens IS NOT NULL
                AND total_tokens = input_tokens + output_tokens
                AND tool_use_prompt_tokens IS NULL
            )
        ),
        CHECK (
            reconciliation_status != 'exact'
            OR (
                input_tokens IS NOT NULL AND output_tokens IS NOT NULL
                AND total_tokens IS NOT NULL
                AND total_tokens = input_tokens + output_tokens
                AND unattributed_tokens IS NULL
            )
        ),
        CHECK (
            reconciliation_status != 'provider_total_only'
            OR (
                total_tokens IS NOT NULL
                AND input_tokens IS NULL AND output_tokens IS NULL
                AND total_derived = 0
            )
        ),
        CHECK (
            reconciliation_status != 'unavailable'
            OR usage_source = 'unavailable'
        ),
        CHECK (
            usage_source != 'unavailable'
            OR (
                base_input_tokens IS NULL AND visible_output_tokens IS NULL
                AND input_tokens IS NULL AND output_tokens IS NULL
                AND total_tokens IS NULL AND cache_read_input_tokens IS NULL
                AND cache_write_input_tokens IS NULL
                AND reasoning_output_tokens IS NULL
                AND tool_use_prompt_tokens IS NULL AND unattributed_tokens IS NULL
                AND provider_cost_microusd IS NULL AND total_derived = 0
                AND reconciliation_status = 'unavailable'
            )
        )
    )
    """,
    "CREATE INDEX idx_run_started ON run(started_at DESC, run_id DESC)",
    "CREATE INDEX idx_run_kind_status_started ON run(run_kind, status, started_at DESC)",
    "CREATE INDEX idx_span_run_sequence ON span(run_id, sequence)",
    "CREATE INDEX idx_span_kind_name_started ON span(span_kind, name, started_at DESC)",
    """
    CREATE INDEX idx_span_provider_model_started
        ON span(provider, requested_model, started_at DESC)
    """,
    """
    CREATE UNIQUE INDEX uq_span_llm_round
        ON span(run_id, llm_role, round_index)
        WHERE span_kind = 'llm_request'
    """,
)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _schema_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute(
            "SELECT MAX(version) AS version FROM telemetry_schema_version"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row["version"] is None:
        return None
    value = row["version"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TelemetrySchemaError("telemetry schema version must be an integer")
    return value


def _migrate(conn: sqlite3.Connection, *, applied_at: datetime) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _schema_version(conn)
        if current is not None and current > CURRENT_TELEMETRY_SCHEMA_VERSION:
            raise TelemetrySchemaError(
                "telemetry database schema is newer than this Kindred version"
            )
        if current is None:
            for statement in _MIGRATION_V1:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO telemetry_schema_version(version, applied_at) VALUES (?, ?)",
                (CURRENT_TELEMETRY_SCHEMA_VERSION, _iso(applied_at)),
            )
        elif current < CURRENT_TELEMETRY_SCHEMA_VERSION:  # pragma: no cover - v2 forward hook
            raise TelemetrySchemaError("telemetry migration path is incomplete")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _preflight_existing_schema(path: Path) -> None:
    """Reject a future DB through a read-only connection before any mutation."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise TelemetrySchemaError("telemetry database path must be a regular file")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(
        uri,
        uri=True,
        timeout=WRITER_BUSY_TIMEOUT_MS / 1000,
    )
    conn.row_factory = sqlite3.Row
    try:
        current = _schema_version(conn)
    finally:
        conn.close()
    if current is not None and current > CURRENT_TELEMETRY_SCHEMA_VERSION:
        raise TelemetrySchemaError("telemetry database schema is newer than this Kindred version")


def _prepare_private_database(path: Path) -> None:
    """Create or tighten the DB before SQLite can create journal sidecars."""

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise TelemetrySchemaError("telemetry database path must be a regular file") from None
        path.chmod(0o600)
    else:
        os.close(descriptor)


def _tighten_existing_sidecars(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise TelemetrySchemaError("telemetry SQLite files must be regular files")
        candidate.chmod(0o600)


class SqliteTelemetrySink:
    """Single-process facade over a WAL telemetry database."""

    _conn: sqlite3.Connection
    _path: Path
    _retention_days: int
    _last_cleanup_date: date | None
    _last_cleanup_attempt_at: datetime | None
    _retention_state_lock: threading.Lock
    _retention_stop: threading.Event
    _retention_thread: threading.Thread | None
    _lock: threading.RLock
    _closed: bool

    def __init__(self) -> None:
        raise RuntimeError("use SqliteTelemetrySink.open()")

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        retention_days: int,
        now: datetime | None = None,
    ) -> SqliteTelemetrySink:
        if isinstance(retention_days, bool) or not 1 <= retention_days <= 3650:
            raise ValueError("retention_days must be between 1 and 3650")
        path.parent.mkdir(parents=True, exist_ok=True)
        _preflight_existing_schema(path)
        _prepare_private_database(path)
        _tighten_existing_sidecars(path)
        conn = sqlite3.connect(
            str(path),
            check_same_thread=False,
            isolation_level=None,
            timeout=WRITER_BUSY_TIMEOUT_MS / 1000,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {WRITER_BUSY_TIMEOUT_MS}")
            _migrate(conn, applied_at=now or datetime.now(tz=timezone.utc))
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            _tighten_existing_sidecars(path)
        except BaseException:
            conn.close()
            raise
        instance = object.__new__(cls)
        instance._conn = conn
        instance._path = path
        instance._retention_days = retention_days
        instance._last_cleanup_date = None
        instance._last_cleanup_attempt_at = None
        instance._retention_state_lock = threading.Lock()
        instance._retention_stop = threading.Event()
        instance._retention_thread = None
        instance._lock = threading.RLock()
        instance._closed = False
        return instance

    @property
    def path(self) -> Path:
        return self._path

    @property
    def schema_version(self) -> int:
        with self._locked():
            self._ensure_open()
            version = _schema_version(self._conn)
            if version is None:  # pragma: no cover - constructor invariant
                raise TelemetrySchemaError("telemetry database is not migrated")
            return version

    @contextmanager
    def _locked(self) -> Iterator[None]:
        acquired = self._lock.acquire(timeout=WRITER_BUSY_TIMEOUT_MS / 1000)
        if not acquired:
            raise TelemetryWriteError("telemetry in-process writer lock timed out")
        try:
            yield
        finally:
            self._lock.release()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._locked():
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def start_run(self, run: RunStart) -> None:
        with self._transaction():
            self._conn.execute(
                """
                INSERT INTO run(
                    run_id, run_kind, execution_mode, trigger_source, dream_date,
                    started_at, status, app_version
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    run.run_id,
                    run.run_kind,
                    run.execution_mode,
                    run.trigger_source,
                    run.dream_date.isoformat() if run.dream_date is not None else None,
                    _iso(run.started_at),
                    run.app_version,
                ),
            )

    def start_llm_request(self, request: LlmRequestStart) -> None:
        stage = request.parent_stage
        with self._transaction():
            self._conn.execute(
                """
                INSERT INTO span(
                    span_id, run_id, sequence, span_kind, name, started_at, status
                ) VALUES (?, ?, ?, 'graph_node', ?, ?, 'running')
                ON CONFLICT(span_id) DO NOTHING
                """,
                (
                    stage.span_id,
                    stage.run_id,
                    stage.sequence,
                    stage.name,
                    _iso(stage.started_at),
                ),
            )
            existing = self._conn.execute(
                """
                SELECT run_id, sequence, span_kind, name, started_at, status
                FROM span WHERE span_id = ?
                """,
                (stage.span_id,),
            ).fetchone()
            expected = (
                stage.run_id,
                stage.sequence,
                "graph_node",
                stage.name,
                _iso(stage.started_at),
                "running",
            )
            if existing is None or tuple(existing) != expected:
                raise TelemetryWriteError("LLM parent stage checkpoint mismatch")
            self._conn.execute(
                """
                INSERT INTO span(
                    span_id, run_id, parent_span_id, sequence, span_kind, name,
                    llm_role, round_index, provider, requested_model, started_at, status
                ) VALUES (?, ?, ?, ?, 'llm_request', ?, ?, ?, ?, ?, ?, 'running')
                """,
                (
                    request.span_id,
                    request.run_id,
                    stage.span_id,
                    request.sequence,
                    request.llm_role,
                    request.llm_role,
                    request.round_index,
                    request.provider,
                    request.requested_model,
                    _iso(request.started_at),
                ),
            )

    def finish_llm_request(self, request: LlmRequestFinish) -> None:
        with self._transaction():
            cursor = self._conn.execute(
                """
                UPDATE span
                SET response_model = ?, ended_at = ?, duration_us = ?, status = ?,
                    error_type = ?, http_status = ?
                WHERE span_id = ? AND run_id = ? AND span_kind = 'llm_request'
                    AND status = 'running'
                """,
                (
                    request.response_model,
                    _iso(request.ended_at),
                    request.duration_us,
                    request.status,
                    request.error_type,
                    request.http_status,
                    request.span_id,
                    request.run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise TelemetryWriteError("LLM finish checkpoint did not match one running request")
            self._insert_usage(request.span_id, request.usage, request.prompt_shape)

    def finish_run(self, run: CompletedRun) -> None:
        graph_spans = sorted(
            (span for span in run.spans if span.span_kind == "graph_node"),
            key=lambda span: span.sequence,
        )
        tool_spans = sorted(
            (span for span in run.spans if span.span_kind == "tool"),
            key=lambda span: span.sequence,
        )
        with self._transaction():
            for span in graph_spans:
                self._upsert_graph_span(span)
            for span in tool_spans:
                self._insert_tool_span(span)
            running = self._conn.execute(
                "SELECT COUNT(*) AS count FROM span WHERE run_id = ? AND status = 'running'",
                (run.run_id,),
            ).fetchone()
            if running is None or running["count"] != 0:
                raise TelemetryWriteError("run cannot finalize with running child spans")
            cursor = self._conn.execute(
                """
                UPDATE run
                SET tick_id = ?, ended_at = ?, duration_us = ?, status = ?, error_type = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (
                    run.tick_id,
                    _iso(run.ended_at),
                    run.duration_us,
                    run.status,
                    run.error_type,
                    run.run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise TelemetryWriteError("run finish did not match one running root")
        self._maybe_cleanup(run.ended_at)

    def cleanup_retention(self, *, now: datetime) -> int:
        if now.tzinfo is None:
            raise ValueError("retention clock must be timezone-aware")
        cutoff = now.astimezone(timezone.utc) - timedelta(days=self._retention_days)
        with self._transaction():
            cursor = self._conn.execute(
                "DELETE FROM run WHERE started_at < ?",
                (_iso(cutoff),),
            )
        return cursor.rowcount

    def _maybe_cleanup(self, now: datetime) -> None:
        utc_now = now.astimezone(timezone.utc)
        utc_date = utc_now.date()
        with self._retention_state_lock:
            if self._retention_stop.is_set():
                return
            if self._last_cleanup_date == utc_date:
                return
            active = self._retention_thread
            if active is not None and active.is_alive():
                return
            if self._last_cleanup_attempt_at is not None:
                since_attempt = utc_now - self._last_cleanup_attempt_at
                if since_attempt < RETENTION_RETRY_INTERVAL:
                    return
            self._last_cleanup_attempt_at = utc_now
            thread = threading.Thread(
                target=self._run_scheduled_cleanup,
                args=(utc_now, utc_date),
                name="kindred-telemetry-retention",
                daemon=True,
            )
            self._retention_thread = thread
            thread.start()

    def _run_scheduled_cleanup(self, now: datetime, utc_date: date) -> None:
        try:
            completed = self._cleanup_retention_in_background(now=now)
        except Exception as exc:  # noqa: BLE001 - maintenance must remain fail-open
            _LOG.warning(
                "telemetry degraded operation=retention error_type=%s",
                type(exc).__name__,
            )
            return
        if completed:
            with self._retention_state_lock:
                self._last_cleanup_date = utc_date
                self._last_cleanup_attempt_at = None

    def _cleanup_retention_in_background(self, *, now: datetime) -> bool:
        """Delete expired roots in short transactions on a maintenance connection."""

        if self._retention_stop.wait(RETENTION_INITIAL_DELAY_SECONDS):
            return False
        cutoff = now - timedelta(days=self._retention_days)
        conn = sqlite3.connect(
            str(self._path),
            isolation_level=None,
            timeout=WRITER_BUSY_TIMEOUT_MS / 1000,
        )
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {WRITER_BUSY_TIMEOUT_MS}")
            while not self._retention_stop.is_set():
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cursor = conn.execute(
                        """
                        DELETE FROM run WHERE run_id IN (
                            SELECT run_id FROM run INDEXED BY idx_run_started
                            WHERE started_at < ?
                            ORDER BY started_at ASC, run_id ASC
                            LIMIT ?
                        )
                        """,
                        (_iso(cutoff), RETENTION_DELETE_BATCH_SIZE),
                    )
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                if cursor.rowcount < RETENTION_DELETE_BATCH_SIZE:
                    return True
                if self._retention_stop.wait(RETENTION_BATCH_PAUSE_SECONDS):
                    return False
            return False
        finally:
            conn.close()

    def _upsert_graph_span(self, span: CompletedSpan) -> None:
        cursor = self._conn.execute(
            """
            INSERT INTO span(
                span_id, run_id, sequence, span_kind, name, started_at,
                ended_at, duration_us, status, error_type
            ) VALUES (?, ?, ?, 'graph_node', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(span_id) DO UPDATE SET
                ended_at = excluded.ended_at,
                duration_us = excluded.duration_us,
                status = excluded.status,
                error_type = excluded.error_type
            WHERE span.run_id = excluded.run_id
                AND span.sequence = excluded.sequence
                AND span.span_kind = 'graph_node'
                AND span.name = excluded.name
                AND span.started_at = excluded.started_at
                AND span.status = 'running'
            """,
            (
                span.span_id,
                span.run_id,
                span.sequence,
                span.name,
                _iso(span.started_at),
                _iso(span.ended_at),
                span.duration_us,
                span.status,
                span.error_type,
            ),
        )
        if cursor.rowcount != 1:
            raise TelemetryWriteError("graph span finalize mismatch")

    def _insert_tool_span(self, span: CompletedSpan) -> None:
        self._conn.execute(
            """
            INSERT INTO span(
                span_id, run_id, parent_span_id, sequence, span_kind, name,
                source_round_index, tool_effect, started_at, ended_at,
                duration_us, status, error_type
            ) VALUES (?, ?, ?, ?, 'tool', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                span.span_id,
                span.run_id,
                span.parent_span_id,
                span.sequence,
                span.name,
                span.source_round_index,
                span.tool_effect,
                _iso(span.started_at),
                _iso(span.ended_at),
                span.duration_us,
                span.status,
                span.error_type,
            ),
        )

    def _insert_usage(
        self,
        span_id: str,
        usage: CanonicalUsage,
        shape: PromptShape,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO llm_usage(
                span_id, usage_source, reconciliation_status,
                base_input_tokens, visible_output_tokens,
                input_tokens, output_tokens, total_tokens, total_derived,
                cache_read_input_tokens, cache_write_input_tokens,
                reasoning_output_tokens, tool_use_prompt_tokens, unattributed_tokens,
                provider_cost_microusd, payload_json_bytes, system_text_chars,
                initial_user_text_chars, tool_schema_json_chars,
                response_schema_json_chars, model_history_json_chars,
                tool_result_json_chars
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                span_id,
                usage.source,
                usage.reconciliation_status,
                usage.base_input_tokens,
                usage.visible_output_tokens,
                usage.input_tokens,
                usage.output_tokens,
                usage.total_tokens,
                int(usage.total_derived),
                usage.cache_read_input_tokens,
                usage.cache_write_input_tokens,
                usage.reasoning_output_tokens,
                usage.tool_use_prompt_tokens,
                usage.unattributed_tokens,
                usage.provider_cost_microusd,
                shape.payload_json_bytes,
                shape.system_text_chars,
                shape.initial_user_text_chars,
                shape.tool_schema_json_chars,
                shape.response_schema_json_chars,
                shape.model_history_json_chars,
                shape.tool_result_json_chars,
            ),
        )

    def close(self) -> None:
        self._retention_stop.set()
        with self._retention_state_lock:
            retention_thread = self._retention_thread
        if retention_thread is not None and retention_thread is not threading.current_thread():
            retention_thread.join(timeout=RETENTION_CLOSE_WAIT_SECONDS)
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("telemetry sink is closed")
