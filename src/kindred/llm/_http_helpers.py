"""Shared JSON, error, and budget mechanics for HTTP LLM clients."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

import httpx

from kindred.llm.client import LlmClientError, ToolLoopError
from kindred.llm.real_client import parse_llm_json, text_fingerprint
from kindred.llm.tools import ToolEvent
from kindred_capability_sdk import ToolDef

if TYPE_CHECKING:
    from kindred.llm.client import Role


@dataclass(frozen=True)
class HttpClientHelpers:
    """Common mechanics parameterized by provider-owned wire fields."""

    logger: logging.Logger
    provider_name: str
    client_error: type[LlmClientError]
    request_items_key: str
    request_result: tuple[str, str]
    usage_fields: Mapping[str, tuple[str, ...]]
    response_usage_key: str = "usage"

    def raise_or_wrap(
        self,
        message: str,
        events: list[ToolEvent],
        rounds: int,
        *,
        raw_text: str | None = None,
    ) -> NoReturn:
        if events:
            raise ToolLoopError(message, tool_events=events, rounds=rounds, raw_text=raw_text)
        raise self.client_error(message, raw_text=raw_text)

    def parse_json(
        self,
        text: str,
        role: Role,
        *,
        protocol_error: Callable[..., LlmClientError],
    ) -> dict[str, Any]:
        try:
            parsed = parse_llm_json(text)
        except json.JSONDecodeError as exc:
            fingerprint = text_fingerprint(text)
            self.logger.warning(
                f"{self.provider_name} response rejected role=%s error_type=invalid_json %s",
                role,
                fingerprint,
            )
            client_name = self.client_error.__name__.removesuffix("Error")
            raise self.client_error(
                f"{client_name}: invalid JSON (role={role}); {fingerprint}", raw_text=text
            ) from exc
        if not isinstance(parsed, dict):
            raise protocol_error(
                f"JSON root is {type(parsed).__name__}, not object",
                role,
                error_type="json_root_not_object",
                fingerprint_text=text,
            )
        return parsed

    def log_request_budget(
        self,
        payload: dict[str, Any],
        role: Role,
        round_index: int,
        max_rounds: int,
        tools: Sequence[ToolDef],
    ) -> None:
        if not self.logger.isEnabledFor(logging.DEBUG):
            return
        items = payload[self.request_items_key]
        result_field, result_value = self.request_result
        history = [item for item in items[2:] if item.get(result_field) != result_value]
        results = [item for item in items[2:] if item.get(result_field) == result_value]
        budget = {
            "role": role,
            "round": round_index,
            "max_rounds": max_rounds,
            "payload_json_bytes": len(compact_json(payload).encode()),
            "system_text_chars": len(items[0]["content"]),
            "initial_user_text_chars": len(items[1]["content"]),
            "tool_schema_json_chars": len(compact_json(payload["tools"])) if tools else 0,
            "response_schema_json_chars": 0,
            "model_history_json_chars": len(compact_json(history)) if history else 0,
            "tool_result_json_chars": len(compact_json(results)) if results else 0,
            "tool_names": tuple(tool.name for tool in tools),
        }
        self.logger.debug(
            f"{self.provider_name} request_budget %s",
            budget,
            extra={"kindred_prompt_budget": budget},
        )

    def log_response_budget(
        self,
        response: httpx.Response | None,
        role: Role,
        round_index: int,
        elapsed_s: float,
    ) -> None:
        if not self.logger.isEnabledFor(logging.DEBUG):
            return
        raw: dict[str, Any] = {}
        if response is not None:
            try:
                value = response.json().get(self.response_usage_key, {})
            except (AttributeError, ValueError):
                value = {}
            raw = value if isinstance(value, dict) else {}
        usage: dict[str, int] = {}
        for field, path in self.usage_fields.items():
            candidate: object = raw
            for part in path:
                candidate = candidate.get(part) if isinstance(candidate, dict) else None
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                usage[field] = candidate
        budget = {
            "role": role,
            "round": round_index,
            "status_code": response.status_code if response is not None else None,
            "elapsed_ms": round(elapsed_s * 1000),
            **usage,
        }
        self.logger.debug(
            f"{self.provider_name} response_budget %s",
            budget,
            extra={"kindred_response_budget": budget},
        )


def compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


__all__ = ["HttpClientHelpers", "compact_json"]
