"""OpenAI Responses API client with bounded tool-loop support."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx

from kindred.llm._http_helpers import (
    HttpClientHelpers,
    ParsedJsonResponse,
)
from kindred.llm._http_helpers import (
    compact_json as _compact_json,
)
from kindred.llm.client import LlmClientError, ToolLoopError
from kindred.llm.real_client import (
    act_tool_loop_system_prompt_for,
    system_prompt_for,
    text_fingerprint,
)
from kindred.llm.tools import (
    ToolEvent,
    ToolHandler,
    ToolLoopResult,
    call_tool_handler,
    effect_for_tool,
    tool_map,
    unknown_tool_result,
)
from kindred.telemetry import LlmRequestObservation, extract_openai_usage, observe_llm_request
from kindred_capability_sdk import ToolCall, ToolDef

if TYPE_CHECKING:
    from kindred.config import KindredLlmConfig
    from kindred.llm.client import Role

_SCHEMA_TYPE_NAMES = frozenset({"OBJECT", "STRING", "NUMBER", "INTEGER", "BOOLEAN", "ARRAY"})
_SAFE_INCOMPLETE_REASONS = frozenset({"content_filter", "max_output_tokens"})
_ProtocolError = Callable[..., LlmClientError]


class OpenAILlmClientError(LlmClientError):
    pass


class OpenAILlmClient:
    """Direct, non-streaming client for ``POST /v1/responses``."""

    _PROVIDER_NAME = "OpenAI"
    _TELEMETRY_PROVIDER = "openai"
    _CLIENT_NAME = "OpenAILlmClient"
    _CLIENT_ERROR: type[LlmClientError] = OpenAILlmClientError
    _API_KEY_ENV = "OPENAI_API_KEY"
    _BASE_URL_ENV = "OPENAI_BASE_URL"
    _DEFAULT_BASE_URL = "https://api.openai.com/v1"
    _DEFAULT_TIMEOUT_S = 60.0
    _INCLUDE_TEXT_VERBOSITY = True
    _INCLUDE_TOOL_STRICT = True
    _INCLUDE_ENCRYPTED_REASONING = False

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        max_output_tokens: int = 25_000,
        timeout_s: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = (base_url or self._DEFAULT_BASE_URL).rstrip("/")
        self._max_output_tokens = max_output_tokens
        self._timeout_s = self._DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
        self._client = httpx.Client(timeout=self._timeout_s, transport=transport)
        self._logger = logging.getLogger(type(self).__module__)
        self._http_helpers = HttpClientHelpers(
            logger=self._logger,
            provider_name=self._PROVIDER_NAME,
            client_error=self._CLIENT_ERROR,
            request_items_key="input",
            request_result=("type", "function_call_output"),
            usage_fields={
                "input_tokens": ("input_tokens",),
                "output_tokens": ("output_tokens",),
                "total_tokens": ("total_tokens",),
                "cached_tokens": ("input_tokens_details", "cached_tokens"),
                "reasoning_tokens": ("output_tokens_details", "reasoning_tokens"),
            },
            usage_extractor=extract_openai_usage,
        )

    @classmethod
    def from_config(cls, llm: KindredLlmConfig) -> OpenAILlmClient:
        api_key = os.environ.get(cls._API_KEY_ENV, "")
        if not api_key:
            raise cls._CLIENT_ERROR(f"{cls._CLIENT_NAME}: missing env {cls._API_KEY_ENV}")
        base_url = os.environ.get(cls._BASE_URL_ENV) or cls._DEFAULT_BASE_URL
        return cls(api_key=api_key, model=llm.model, base_url=base_url)

    def complete(self, prompt: str, *, role: Role) -> dict[str, Any]:
        input_items = _initial_input(system_prompt_for(role), prompt)
        payload = self._payload(input_items=input_items, tools=())
        with observe_llm_request(
            role=role,
            round_index=1,
            provider=self._TELEMETRY_PROVIDER,
            requested_model=self._model,
            prompt_shape_factory=partial(self._http_helpers.extract_prompt_shape, payload, ()),
        ) as observation:
            response = self._post(
                payload,
                role=role,
                round_index=1,
                max_rounds=1,
                tools=(),
                observation=observation,
            )
            output = _response_output(response, role, self._protocol_error)
            if any(item.get("type") == "function_call" for item in output):
                raise self._protocol_error("response contains an unexpected function call", role)
            return self._http_helpers.parse_json(
                _output_text(output, role, self._protocol_error),
                role,
                protocol_error=self._protocol_error,
            )
        raise AssertionError("LLM request observation suppressed control flow")

    def close(self) -> None:
        self._client.close()

    def complete_with_tools(
        self,
        prompt: str,
        *,
        role: Role,
        tools: Sequence[ToolDef],
        handler: ToolHandler,
        max_rounds: int,
    ) -> ToolLoopResult:
        if max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")
        known_tools = tool_map(tuple(tools))
        system = (
            act_tool_loop_system_prompt_for(tools) if role == "act.llm" else system_prompt_for(role)
        )
        input_items = _initial_input(system, prompt)
        events: list[ToolEvent] = []

        for round_index in range(1, max_rounds + 1):
            payload = self._payload(input_items=input_items, tools=tools)
            final: dict[str, Any] | None = None
            try:
                with observe_llm_request(
                    role=role,
                    round_index=round_index,
                    provider=self._TELEMETRY_PROVIDER,
                    requested_model=self._model,
                    prompt_shape_factory=partial(
                        self._http_helpers.extract_prompt_shape, payload, tools
                    ),
                ) as observation:
                    response = self._post(
                        payload,
                        role=role,
                        round_index=round_index,
                        max_rounds=max_rounds,
                        tools=tools,
                        observation=observation,
                    )
                    output = _response_output(response, role, self._protocol_error)
                    calls = _parse_tool_calls(output, role, self._protocol_error)
                    if not calls:
                        try:
                            final = self._http_helpers.parse_json(
                                _output_text(output, role, self._protocol_error),
                                role,
                                protocol_error=self._protocol_error,
                            )
                        except LlmClientError as exc:
                            raise ToolLoopError(
                                f"{self._CLIENT_NAME}: invalid tool-loop final JSON (role={role})",
                                tool_events=events,
                                rounds=round_index,
                                raw_text=exc.raw_text,
                            ) from exc
            except ToolLoopError:
                raise
            except LlmClientError as exc:
                self._http_helpers.raise_or_wrap(
                    str(exc), events, round_index - 1, raw_text=exc.raw_text
                )

            if calls:
                input_items.extend(output)
                for call in calls:
                    result = (
                        call_tool_handler(
                            handler,
                            call,
                            tool_def=known_tools[call.name],
                            round_index=round_index,
                        )
                        if call.name in known_tools
                        else unknown_tool_result(call, round_index=round_index)
                    )
                    events.append(
                        ToolEvent(
                            round_index=round_index,
                            call=call,
                            result=result,
                            effect=effect_for_tool(known_tools, call.name),
                        )
                    )
                    input_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": _compact_json(result.response),
                        }
                    )
                continue

            assert final is not None
            return ToolLoopResult(final=final, tool_events=tuple(events), rounds=round_index)

        raise ToolLoopError(
            f"{self._CLIENT_NAME}: reached max_rounds={max_rounds} without final",
            tool_events=events,
            rounds=max_rounds,
        )

    def _payload(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: Sequence[ToolDef],
    ) -> dict[str, Any]:
        text: dict[str, Any] = {"format": {"type": "json_object"}}
        if self._INCLUDE_TEXT_VERBOSITY:
            text["verbosity"] = "low"
        payload: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            "text": text,
            "reasoning": {"effort": "high"},
            "max_output_tokens": self._max_output_tokens,
            "store": False,
            "stream": False,
        }
        if tools:
            declarations = [_openai_function_declaration(tool) for tool in tools]
            if not self._INCLUDE_TOOL_STRICT:
                for declaration in declarations:
                    declaration.pop("strict")
            payload["tools"] = declarations
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = True
            if self._INCLUDE_ENCRYPTED_REASONING:
                payload["include"] = ["reasoning.encrypted_content"]
        return payload

    def _post(
        self,
        payload: dict[str, Any],
        *,
        role: Role,
        round_index: int,
        max_rounds: int,
        tools: Sequence[ToolDef],
        observation: LlmRequestObservation,
    ) -> ParsedJsonResponse:
        started: float | None = None
        raw_response: httpx.Response | None = None
        transport_error_type: str | None = None
        try:
            request = self._client.build_request(
                "POST",
                f"{self._base_url}/responses",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
            payload_json_bytes = len(request.content)
            observation.record_payload_json_bytes(payload_json_bytes)
            self._http_helpers.log_request_budget(
                payload,
                role,
                round_index,
                max_rounds,
                tools,
                payload_json_bytes=payload_json_bytes,
            )
            observation.mark_request_started()
            started = time.perf_counter()
            raw_response = self._client.send(request)
        except httpx.HTTPError as exc:
            self._http_helpers.log_response_budget(
                None,
                role,
                round_index,
                0.0 if started is None else time.perf_counter() - started,
            )
            transport_error_type = type(exc).__name__
        if raw_response is None:
            assert transport_error_type is not None
            raise self._CLIENT_ERROR(
                f"{self._CLIENT_NAME}: HTTP request failed "
                f"(role={role}, model={self._model}, error_type={transport_error_type})"
            )
        if raw_response.status_code != httpx.codes.OK:
            self._http_helpers.log_response_budget(
                None,
                role,
                round_index,
                0.0 if started is None else time.perf_counter() - started,
                status_code=raw_response.status_code,
            )
            observation.record_http_status(raw_response.status_code)
            raise self._CLIENT_ERROR(
                f"{self._CLIENT_NAME}: HTTP {raw_response.status_code} "
                f"(role={role}, model={self._model}, response_bytes={len(raw_response.content)})"
            )
        response = ParsedJsonResponse.parse(raw_response)
        self._http_helpers.log_response_budget(
            response,
            role,
            round_index,
            0.0 if started is None else time.perf_counter() - started,
        )
        observation.record_response(
            http_status=raw_response.status_code,
            usage_factory=lambda: self._http_helpers.canonical_usage(response),
            response_model_factory=lambda: self._http_helpers.response_model(response),
        )
        return response

    def _protocol_error(
        self,
        reason: str,
        role: Role,
        *,
        error_type: str = "invalid_response",
        fingerprint_text: str | None = None,
    ) -> LlmClientError:
        fingerprint = (
            f" {text_fingerprint(fingerprint_text)}" if fingerprint_text is not None else ""
        )
        self._logger.warning(
            "%s response rejected role=%s error_type=%s%s",
            self._PROVIDER_NAME,
            role,
            error_type,
            fingerprint,
        )
        return self._CLIENT_ERROR(f"{self._CLIENT_NAME}: {reason} (role={role})")


def _initial_input(system: str, prompt: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]


def _response_output(
    response: ParsedJsonResponse,
    role: Role,
    protocol_error: _ProtocolError,
) -> list[dict[str, Any]]:
    if response.json_error:
        raise protocol_error("response is not JSON", role)
    body = response.body
    if not isinstance(body, dict):
        raise protocol_error("response root is not object", role)
    status = body.get("status")
    if status != "completed":
        if status == "incomplete":
            details = body.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            if reason in _SAFE_INCOMPLETE_REASONS:
                raise protocol_error(
                    f"response incomplete ({reason})",
                    role,
                    error_type=f"incomplete_{reason}",
                )
            raise protocol_error("response is incomplete", role, error_type="incomplete")
        error_type = "failed" if status == "failed" else "invalid_status"
        raise protocol_error("response status is not completed", role, error_type=error_type)
    output = body.get("output")
    if not isinstance(output, list) or not output:
        raise protocol_error("response has no output", role)
    if not all(isinstance(item, dict) for item in output):
        raise protocol_error("response output contains a non-object item", role)
    return output


def _output_text(output: list[dict[str, Any]], role: Role, protocol_error: _ProtocolError) -> str:
    messages = [item for item in output if item.get("type") == "message"]
    if len(messages) != 1 or messages[0].get("role") != "assistant":
        raise protocol_error("response has no unique assistant message", role)
    content = messages[0].get("content")
    if not isinstance(content, list) or not content:
        raise protocol_error("assistant message has no content", role)
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            raise protocol_error("assistant content contains a non-object item", role)
        if item.get("type") == "refusal":
            raise protocol_error("assistant refused the request", role, error_type="refusal")
        if item.get("type") != "output_text":
            raise protocol_error("assistant content has an unsupported type", role)
        text = item.get("text")
        if not isinstance(text, str):
            raise protocol_error("assistant output text is invalid", role)
        parts.append(text)
    value = "".join(parts)
    if not value.strip():
        raise protocol_error(
            "response has no non-empty content",
            role,
            error_type="empty_content",
            fingerprint_text=value,
        )
    return value


def _required_string(value: object, field: str, role: Role, protocol_error: _ProtocolError) -> str:
    if not isinstance(value, str) or not value:
        raise protocol_error(f"invalid {field}", role)
    return value


def _parse_tool_calls(
    output: list[dict[str, Any]], role: Role, protocol_error: _ProtocolError
) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for item in output:
        if item.get("type") != "function_call":
            continue
        call_id = _required_string(item.get("call_id"), "tool call id", role, protocol_error)
        name = _required_string(item.get("name"), "tool name", role, protocol_error)
        arguments = _required_string(item.get("arguments"), "tool arguments", role, protocol_error)
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise protocol_error("tool arguments are not JSON", role) from exc
        if not isinstance(args, dict):
            raise protocol_error("tool arguments root is not object", role)
        calls.append(ToolCall(name=name, args=args, call_id=call_id))
    return calls


def _openai_function_declaration(tool: ToolDef) -> dict[str, Any]:
    declaration = tool.function_declaration()
    parameters = declaration["parameters"]
    stack = [parameters]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            schema_type = value.get("type")
            if isinstance(schema_type, str) and schema_type in _SCHEMA_TYPE_NAMES:
                value["type"] = schema_type.lower()
            if value.pop("nullable", False) is True and isinstance(value.get("type"), str):
                value["type"] = [value["type"], "null"]
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return {
        "type": "function",
        "name": declaration["name"],
        "description": declaration["description"],
        "parameters": parameters,
        "strict": False,
    }


__all__ = ["OpenAILlmClient", "OpenAILlmClientError"]
