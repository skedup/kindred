"""Narrow, host-neutral transcript and outbound runtime contracts."""

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class TranscriptMessage:
    msg_id: str
    seq: int | None
    role: str
    text_summary: str | None
    ts_ms: int


@dataclass(frozen=True, slots=True)
class TranscriptBatch:
    identity: str
    messages: tuple[TranscriptMessage, ...]


class TranscriptSource(Protocol):
    def pull(self) -> TranscriptBatch: ...


class TranscriptPullError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class DispatchResult:
    status: Literal["accepted", "failed", "unknown"]


class OutboundChannel(Protocol):
    def send(self, text: str, *, artifact_ref: str) -> DispatchResult: ...
