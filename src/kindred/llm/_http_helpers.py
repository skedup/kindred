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
from kindred.telemetry import CanonicalUsage, PromptShape, safe_response_model
from kindred.telemetry.usage import UsageExtractor
from kindred_capability_sdk import ToolDef

if TYPE_CHECKING:
    from kindred.llm.client import Role


@dataclass(frozen=True)
class ParsedJsonResponse:
    """One decoded response body shared by business and telemetry projections."""

    response: httpx.Response
    body: object
    json_error: bool = False

    @classmethod
    def parse(cls, response: httpx.Response) -> ParsedJsonResponse:
        try:
            body = response.json()
        except (AttributeError, ValueError):
            return cls(response=response, body=None, json_error=True)
        return cls(response=response, body=body)


@dataclass(frozen=True)
class HttpClientHelpers:
    """Common mechanics parameterized by provider-owned wire fields."""

    logger: logging.Logger
    provider_name: str
    client_error: type[LlmClientError]
    request_items_key: str
    request_result: tuple[str, str]
    usage_fields: Mapping[str, tuple[str, ...]]
    usage_extractor: UsageExtractor
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
        *,
        payload_json_bytes: int | None = None,
    ) -> None:
        if not self.logger.isEnabledFor(logging.DEBUG):
            return
        try:
            shape = self.extract_prompt_shape(
                payload,
                tools,
                payload_json_bytes=payload_json_bytes,
            )
        except Exception as exc:  # noqa: BLE001 - debug projection must be fail-open
            self.logger.debug(
                f"{self.provider_name} request_budget unavailable error_type=%s",
                type(exc).__name__,
            )
            return
        budget = {
            "role": role,
            "round": round_index,
            "max_rounds": max_rounds,
            **shape.__dict__,
            "tool_names": tuple(tool.name for tool in tools),
        }
        self.logger.debug(
            f"{self.provider_name} request_budget %s",
            budget,
            extra={"kindred_prompt_budget": budget},
        )

    def extract_prompt_shape(
        self,
        payload: dict[str, Any],
        tools: Sequence[ToolDef],
        *,
        payload_json_bytes: int | None = None,
    ) -> PromptShape:
        """Extract dimensions without materializing another complete request body."""

        items = payload[self.request_items_key]
        result_field, result_value = self.request_result
        history = [item for item in items[2:] if item.get(result_field) != result_value]
        results = [item for item in items[2:] if item.get(result_field) == result_value]
        return PromptShape(
            payload_json_bytes=payload_json_bytes,
            system_text_chars=len(items[0]["content"]),
            initial_user_text_chars=len(items[1]["content"]),
            tool_schema_json_chars=compact_json_chars(payload["tools"]) if tools else 0,
            response_schema_json_chars=0,
            model_history_json_chars=compact_json_chars(history) if history else 0,
            tool_result_json_chars=compact_json_chars(results) if results else 0,
        )

    def canonical_usage(self, response: ParsedJsonResponse) -> CanonicalUsage:
        return self.usage_extractor(self._response_usage(response))

    def response_model(self, response: ParsedJsonResponse) -> str | None:
        body = response.body
        if not isinstance(body, dict):
            return None
        return safe_response_model(body.get("model"))

    def _response_usage(self, response: ParsedJsonResponse) -> object:
        body = response.body
        return body.get(self.response_usage_key) if isinstance(body, dict) else None

    def log_response_budget(
        self,
        response: ParsedJsonResponse | None,
        role: Role,
        round_index: int,
        elapsed_s: float,
        *,
        status_code: int | None = None,
    ) -> None:
        if not self.logger.isEnabledFor(logging.DEBUG):
            return
        usage: dict[str, int] = {}
        if response is not None:
            raw = self._response_usage(response)
            for field, path in self.usage_fields.items():
                candidate = raw
                for part in path:
                    candidate = candidate.get(part) if isinstance(candidate, dict) else None
                if (
                    isinstance(candidate, int)
                    and not isinstance(candidate, bool)
                    and candidate >= 0
                ):
                    usage[field] = candidate
        budget = {
            "role": role,
            "round": round_index,
            "status_code": response.response.status_code if response is not None else status_code,
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


def _json_string_chars(value: str) -> int:
    size = 2
    for character in value:
        codepoint = ord(character)
        if character in {'"', "\\"} or character in {"\b", "\f", "\n", "\r", "\t"}:
            size += 2
        elif codepoint < 0x20:
            size += 6
        else:
            size += 1
    return size


def _json_key_chars(value: object) -> int:
    if isinstance(value, str):
        return _json_string_chars(value)
    if value is None or isinstance(value, (bool, int, float)):
        return _json_string_chars(compact_json(value))
    raise TypeError(f"unsupported JSON object key: {type(value).__name__}")


def compact_json_chars(value: object) -> int:
    """Return compact JSON character length without building the JSON string."""

    total = 0
    pending = [value]
    while pending:
        item = pending.pop()
        if item is None or item is True:
            total += 4
        elif item is False:
            total += 5
        elif isinstance(item, str):
            total += _json_string_chars(item)
        elif isinstance(item, int):
            total += len(str(item))
        elif isinstance(item, float):
            total += len(compact_json(item))
        elif isinstance(item, (list, tuple)):
            total += 2 + max(0, len(item) - 1)
            pending.extend(item)
        elif isinstance(item, dict):
            total += 2 + max(0, len(item) - 1)
            for key, nested in item.items():
                total += _json_key_chars(key) + 1
                pending.append(nested)
        else:
            raise TypeError(f"unsupported JSON value: {type(item).__name__}")
    return total


__all__ = [
    "HttpClientHelpers",
    "ParsedJsonResponse",
    "compact_json",
    "compact_json_chars",
]
