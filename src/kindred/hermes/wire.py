from dataclasses import dataclass
from pathlib import Path

HermesRuntimeIdentity = tuple[str, str]

HERMES_BASELINE: HermesRuntimeIdentity = ("v2026.8.18", "0.20.4")


@dataclass(frozen=True, slots=True)
class HermesWire:
    host_executable: Path
    host_home: Path
    approved_platform: str
    approved_sender_id: str
    canonical_session_id: str
    canonical_session_key: str
    outbound_chat_id: str
    outbound_sender_id: str
    outbound_thread_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.host_executable, Path) or not isinstance(self.host_home, Path):
            raise ValueError("Hermes host paths must be Path values")
        text = (
            self.approved_platform,
            self.approved_sender_id,
            self.canonical_session_id,
            self.canonical_session_key,
            self.outbound_chat_id,
            self.outbound_sender_id,
        )
        if any(
            type(value) is not str or not value.strip() or value != value.strip() for value in text
        ):
            raise ValueError("Hermes wire text fields must be non-empty and unpadded")
        if type(self.outbound_thread_id) not in {str, type(None)}:
            raise ValueError("Hermes thread id must be text or null")
        if self.outbound_thread_id is not None and (
            not self.outbound_thread_id.strip()
            or self.outbound_thread_id != self.outbound_thread_id.strip()
        ):
            raise ValueError("Hermes thread id must be non-empty and unpadded")
        if not self.host_executable.is_absolute() or not self.host_home.is_absolute():
            raise ValueError("Hermes host paths must be absolute")
        if self.outbound_sender_id != self.approved_sender_id:
            raise ValueError("Hermes outbound target does not match approved peer")
        route = (self.approved_platform, self.outbound_chat_id, self.outbound_thread_id)
        if self.approved_platform == "cli" or any(part and ":" in part for part in route):
            raise ValueError("Hermes route is not an approved direct target")

    @property
    def outbound_target(self) -> str:
        thread = f":{self.outbound_thread_id}" if self.outbound_thread_id else ""
        return f"{self.approved_platform}:{self.outbound_chat_id}{thread}"
