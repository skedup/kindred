import json
import logging
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from functools import partial
from math import isfinite
from pathlib import Path
from typing import cast

from kindred.db.messages import extract_text_summary, protocol_role_to_business
from kindred.hermes.wire import HermesWire
from kindred.mouth_host.runtime import (
    DispatchResult,
    TranscriptBatch,
    TranscriptMessage,
    TranscriptPullError,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 5.0
COMMAND_TIMEOUT_S = 10.0
MAX_STDOUT_BYTES = 1024 * 1024


CommandRunner = Callable[[tuple[str, ...], bytes | None], tuple[int, bytes]]


class _RunError(RuntimeError):
    def __init__(self, reason: str, *, started: bool) -> None:
        super().__init__(reason)
        self.reason, self.started = reason, started


def run_hermes_command(
    executable: Path,
    home: Path,
    argv: tuple[str, ...],
    stdin: bytes | None = None,
) -> tuple[int, bytes]:
    allowed = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update({"HOME": str(home), "HERMES_HOME": str(home)})
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(  # noqa: S603 - argv comes from strict Hermes wire
                argv,
                input=stdin,
                stdout=output,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=COMMAND_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise _RunError("timeout", started=True) from exc
        except OSError as exc:
            raise _RunError("process_not_started", started=False) from exc
        if output.tell() > MAX_STDOUT_BYTES:
            raise _RunError("stdout_limit", started=True)
        output.seek(0)
        return result.returncode, output.read()


def _run_command(wire: HermesWire, argv: tuple[str, ...], stdin: bytes | None) -> tuple[int, bytes]:
    return run_hermes_command(wire.host_executable, wire.host_home, argv, stdin)


class HermesTranscriptSource:
    def __init__(
        self,
        wire: HermesWire,
        *,
        runner: CommandRunner | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._wire, self._run, self._clock = wire, runner or partial(_run_command, wire), clock
        self._last_attempt: float | None = None
        self._last_batch: TranscriptBatch | None = None

    def pull(self) -> TranscriptBatch:
        now = self._clock()
        if self._last_attempt is not None and now - self._last_attempt < POLL_INTERVAL_S:
            if self._last_batch is None:
                raise TranscriptPullError("poll_throttled")
            return self._last_batch
        self._last_attempt = now
        if not _host_ready(self._wire, "state.db"):
            raise TranscriptPullError("host_preflight_failed")
        try:
            session_id = self._wire.canonical_session_id
            args = "sessions", "export", "-", "--session-id", session_id, "--format", "jsonl"
            returncode, stdout = self._run((str(self._wire.host_executable), *args), None)
        except _RunError as exc:
            raise TranscriptPullError(exc.reason) from None
        if returncode != 0:
            raise TranscriptPullError("cli_failed")
        self._last_batch = _parse_export(stdout, self._wire)
        return self._last_batch


class HermesOutboundChannel:
    def __init__(self, wire: HermesWire, *, runner: CommandRunner | None = None) -> None:
        self._wire, self._run = wire, runner or partial(_run_command, wire)

    def send(self, text: str, *, artifact_ref: str) -> DispatchResult:
        del artifact_ref  # Hermes has no idempotency-key contract.
        if not _host_ready(self._wire, "config.yaml"):
            return DispatchResult(status="failed")
        try:
            args = "send", "--to", self._wire.outbound_target, "--json"
            returncode, stdout = self._run((str(self._wire.host_executable), *args), text.encode())
        except _RunError as exc:
            return DispatchResult(status="unknown" if exc.started else "failed")
        if returncode != 0:
            return DispatchResult(status="unknown")
        try:
            payload = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return DispatchResult(status="unknown")
        if not isinstance(payload, dict) or payload.get("success") is not True:
            return DispatchResult(status="unknown")
        if payload.get("mirrored") is not True:
            logger.warning("hermes outbound: accepted; transcript continuity unavailable")
        return DispatchResult(status="accepted")


def _host_ready(wire: HermesWire, required_name: str) -> bool:
    executable, home = wire.host_executable, wire.host_home
    required = home / required_name
    return (
        home.is_dir()
        and executable.is_file()
        and required.is_file()
        and all(
            os.access(path, mode)
            for path, mode in (
                (home, os.R_OK | os.X_OK),
                (executable, os.X_OK),
                (required, os.R_OK),
            )
        )
    )


def _parse_export(raw: bytes, wire: HermesWire) -> TranscriptBatch:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TranscriptPullError("export_schema_invalid") from None
    expected = {
        "id": wire.canonical_session_id,
        "session_key": wire.canonical_session_key,
        "source": wire.approved_platform,
        "user_id": wire.approved_sender_id,
        "chat_id": wire.outbound_chat_id,
        "chat_type": "dm",
        "thread_id": wire.outbound_thread_id,
    }
    if not isinstance(payload, dict) or any(
        key not in payload or payload[key] != value for key, value in expected.items()
    ):
        raise TranscriptPullError("canonical_identity_mismatch")
    rows = payload.get("messages")
    if not isinstance(rows, list):
        raise TranscriptPullError("export_schema_invalid")
    messages: list[TranscriptMessage] = []
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise TranscriptPullError("export_schema_invalid")
        row_id, role, timestamp = row.get("id"), row.get("role"), row.get("timestamp")
        if type(row_id) is not int or row_id <= 0 or row_id in seen:
            raise TranscriptPullError("export_schema_invalid")
        seen.add(row_id)
        if role in {"system", "tool"}:
            continue
        if role not in {"user", "assistant"} or type(timestamp) not in {int, float}:
            raise TranscriptPullError("export_schema_invalid")
        timestamp_value = float(cast(int | float, timestamp))
        if not isfinite(timestamp_value) or timestamp_value < 0:
            raise TranscriptPullError("export_schema_invalid")
        messages.append(
            TranscriptMessage(
                msg_id=str(row_id),
                seq=row_id,
                role=protocol_role_to_business(role),
                text_summary=extract_text_summary(row.get("content")),
                ts_ms=round(timestamp_value * 1000),
            )
        )
    messages.sort(key=lambda message: message.seq or 0)
    return TranscriptBatch(identity=wire.canonical_session_id, messages=tuple(messages))
