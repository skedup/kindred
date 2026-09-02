"""Daemon-only, fail-open warnings for explicitly configured telemetry thresholds."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Final

from kindred.telemetry.layout import TelemetryLayout
from kindred.telemetry.query import TelemetryQueryService
from kindred.telemetry.query_contracts import TelemetryBudgetStatus

if TYPE_CHECKING:
    from kindred.config import KindredConfig
    from kindred.telemetry.facade import TelemetryFacade

_LOG = logging.getLogger(__name__)

THRESHOLD_WARNING_INTERVAL_SECONDS: Final[float] = 60.0 * 60.0
_CURSOR_KEY_DOMAIN: Final[bytes] = b"kindred-runtime-threshold-monitor-v1\0"


class TelemetryThresholdMonitor:
    """Poll a safe budget projection and warn at most once per metric per hour."""

    def __init__(
        self,
        read_budget: Callable[[], TelemetryBudgetStatus],
        *,
        warning_interval_seconds: float = THRESHOLD_WARNING_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if warning_interval_seconds <= 0:
            raise ValueError("warning_interval_seconds must be positive")
        self._read_budget = read_budget
        self._warning_interval_seconds = warning_interval_seconds
        self._monotonic = monotonic
        self._last_warning: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self) -> None:
        """Evaluate all configured thresholds without allowing failures to escape."""

        try:
            budget = self._read_budget()
        except Exception as exc:  # noqa: BLE001 - monitoring must not change daemon behavior
            self._warn_degraded(type(exc).__name__)
            return

        if budget.daily_token_exceeded is True and budget.daily_token_warn is not None:
            self._warn_exceeded("daily_total_tokens", str(budget.daily_token_warn))
        if budget.tick_duration_exceeded is True and budget.tick_duration_warn_seconds is not None:
            self._warn_exceeded(
                "tick_duration_p95_seconds",
                str(budget.tick_duration_warn_seconds),
            )
        if (
            budget.dream_duration_exceeded is True
            and budget.dream_duration_warn_seconds is not None
        ):
            self._warn_exceeded(
                "dream_duration_p95_seconds",
                str(budget.dream_duration_warn_seconds),
            )

    def _warn_exceeded(self, metric: str, threshold: str) -> None:
        if not self._claim_warning(metric):
            return
        _LOG.warning(
            "telemetry threshold exceeded metric=%s threshold=%s",
            metric,
            threshold,
        )

    def _warn_degraded(self, error_type: str) -> None:
        if not self._claim_warning("threshold_check"):
            return
        _LOG.warning(
            "telemetry degraded operation=threshold_check error_type=%s",
            error_type,
        )

    def _claim_warning(self, key: str) -> bool:
        now = self._monotonic()
        with self._lock:
            previous = self._last_warning.get(key)
            if previous is not None and now - previous < self._warning_interval_seconds:
                return False
            self._last_warning[key] = now
            return True


def create_runtime_threshold_monitor(
    config: KindredConfig,
    *,
    telemetry: TelemetryFacade | None,
) -> TelemetryThresholdMonitor | None:
    """Build an opt-in monitor only when recording and a threshold are configured."""

    observability = config.observability
    thresholds = (
        observability.daily_token_warn,
        observability.tick_duration_warn_seconds,
        observability.dream_duration_warn_seconds,
    )
    if (
        telemetry is None
        or not telemetry.is_recording
        or all(value is None for value in thresholds)
    ):
        return None

    path = TelemetryLayout.from_life_root(config.paths.life_root).db_path
    cursor_key = hashlib.sha256(_CURSOR_KEY_DOMAIN + str(path).encode("utf-8")).digest()
    query = TelemetryQueryService(
        path,
        cursor_key=cursor_key,
        daily_token_warn=observability.daily_token_warn,
        tick_duration_warn_seconds=observability.tick_duration_warn_seconds,
        dream_duration_warn_seconds=observability.dream_duration_warn_seconds,
    )
    return TelemetryThresholdMonitor(lambda: query.summary("24h").budget)


__all__ = [
    "THRESHOLD_WARNING_INTERVAL_SECONDS",
    "TelemetryThresholdMonitor",
    "create_runtime_threshold_monitor",
]
