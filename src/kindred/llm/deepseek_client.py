"""DeepSeek Chat Completions client with bounded tool-loop support."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Sequence
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
from kindred.telemetry import (
    LlmRequestObservation,
    extract_deepseek_usage,
    observe_llm_request,
)
from kindred_capability_sdk import ToolCall, ToolDef

if TYPE_CHECKING:
    from kindred.config import KindredLlmConfig
    from kindred.llm.client import Role

_LOG = logging.getLogger(__name__)
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_DEEPSEEK_REASONING_EFFORT = "high"
_KNOWN_FINISH_REASONS = frozenset(
    {"stop", "tool_calls", "length", "content_filter", "insufficient_system_resource"}
)
_SCHEMA_TYPE_NAMES = frozenset({"OBJECT", "STRING", "NUMBER", "INTEGER", "BOOLEAN", "ARRAY"})


class DeepSeekLlmClientError(LlmClientError):
    pass


_HTTP_HELPERS = HttpClientHelpers(
    logger=_LOG,
    provider_name="DeepSeek",
    client_error=DeepSeekLlmClientError,
    request_items_key="messages",
    request_result=("role", "tool"),
    usage_fields={
        "prompt_tokens": ("prompt_tokens",),
        "prompt_cache_hit_tokens": ("prompt_cache_hit_tokens",),
        "prompt_cache_miss_tokens": ("prompt_cache_miss_tokens",),
        "completion_tokens": ("completion_tokens",),
        "total_tokens": ("total_tokens",),
        "reasoning_tokens": ("completion_tokens_details", "reasoning_tokens"),
    },
    usage_extractor=extract_deepseek_usage,
)


class DeepSeekLlmClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_DEEPSEEK_MODEL,
        base_url: str = "https://api.deepseek.com",
        max_tokens: int = 8192,
        timeout_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._max_tokens = max_tokens
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    @classmethod
    def from_config(cls, llm: KindredLlmConfig) -> DeepSeekLlmClient:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise DeepSeekLlmClientError("DeepSeekLlmClient: missing env DEEPSEEK_API_KEY")
        base_url = os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
        return cls(api_key=api_key, model=llm.model, base_url=base_url)

    def complete(self, prompt: str, *, role: Role) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": system_prompt_for(role)},
            {"role": "user", "content": prompt},
        ]
        payload = self._payload(messages=messages, tools=())
        with observe_llm_request(
            role=role,
            round_index=1,
            provider="deepseek",
            requested_model=self._model,
            prompt_shape_factory=partial(_HTTP_HELPERS.extract_prompt_shape, payload, ()),
        ) as observation:
            response = self._post(
                payload,
                role=role,
                round_index=1,
                max_rounds=1,
                tools=(),
                observation=observation,
            )
            choice, message = _extract_choice(response, role)
            finish = choice.get("finish_reason")
            if finish != "stop":
                raise _protocol_error(
                    f"unexpected finish={_finish_label(finish)}",
                    role,
                    error_type="unexpected_finish",
                )
            return _HTTP_HELPERS.parse_json(
                _content(message, role), role, protocol_error=_protocol_error
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
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        events: list[ToolEvent] = []

        for round_index in range(1, max_rounds + 1):
            payload = self._payload(messages=messages, tools=tools)
            final: dict[str, Any] | None = None
            assistant: dict[str, Any] | None = None
            try:
                with observe_llm_request(
                    role=role,
                    round_index=round_index,
                    provider="deepseek",
                    requested_model=self._model,
                    prompt_shape_factory=partial(
                        _HTTP_HELPERS.extract_prompt_shape, payload, tools
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
                    choice, message = _extract_choice(response, role)
                    finish = choice.get("finish_reason")
                    raw_calls = message.get("tool_calls")
                    if finish == "tool_calls":
                        calls, assistant = _parse_tool_calls(message, role)
                    else:
                        calls = []
                        if finish != "stop":
                            _HTTP_HELPERS.raise_or_wrap(
                                "DeepSeekLlmClient: unexpected "
                                f"finish={_finish_label(finish)} (role={role})",
                                events,
                                round_index - 1,
                            )
                        if raw_calls not in (None, []):
                            _HTTP_HELPERS.raise_or_wrap(
                                "DeepSeekLlmClient: stop response contains "
                                f"tool_calls (role={role})",
                                events,
                                round_index - 1,
                            )
                        try:
                            final = _HTTP_HELPERS.parse_json(
                                _content(message, role), role, protocol_error=_protocol_error
                            )
                        except DeepSeekLlmClientError as exc:
                            raise ToolLoopError(
                                f"DeepSeekLlmClient: invalid tool-loop final JSON (role={role})",
                                tool_events=events,
                                rounds=round_index,
                                raw_text=exc.raw_text,
                            ) from exc
            except ToolLoopError:
                raise
            except DeepSeekLlmClientError as exc:
                _HTTP_HELPERS.raise_or_wrap(
                    str(exc), events, round_index - 1, raw_text=exc.raw_text
                )

            if calls:
                assert assistant is not None
                messages.append(assistant)
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
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id,
                            "content": _compact_json(result.response),
                        }
                    )
                continue

            assert final is not None
            return ToolLoopResult(final=final, tool_events=tuple(events), rounds=round_index)

        raise ToolLoopError(
            f"DeepSeekLlmClient: reached max_rounds={max_rounds} without final",
            tool_events=events,
            rounds=max_rounds,
        )

    def _payload(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: Sequence[ToolDef],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "thinking": {"type": "enabled"},
            "reasoning_effort": DEFAULT_DEEPSEEK_REASONING_EFFORT,
            "response_format": {"type": "json_object"},
            "max_tokens": self._max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": _deepseek_function_declaration(tool)}
                for tool in tools
            ]
            payload["tool_choice"] = "auto"
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
                f"{self._base_url}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
            payload_json_bytes = len(request.content)
            observation.record_payload_json_bytes(payload_json_bytes)
            _HTTP_HELPERS.log_request_budget(
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
            _HTTP_HELPERS.log_response_budget(
                None,
                role,
                round_index,
                0.0 if started is None else time.perf_counter() - started,
            )
            transport_error_type = type(exc).__name__
        if raw_response is None:
            assert transport_error_type is not None
            raise DeepSeekLlmClientError(
                "DeepSeekLlmClient: HTTP request failed "
                f"(role={role}, model={self._model}, error_type={transport_error_type})"
            )
        if raw_response.status_code != httpx.codes.OK:
            _HTTP_HELPERS.log_response_budget(
                None,
                role,
                round_index,
                0.0 if started is None else time.perf_counter() - started,
                status_code=raw_response.status_code,
            )
            observation.record_http_status(raw_response.status_code)
            raise DeepSeekLlmClientError(
                f"DeepSeekLlmClient: HTTP {raw_response.status_code} "
                f"(role={role}, model={self._model}, response_bytes={len(raw_response.content)})"
            )
        response = ParsedJsonResponse.parse(raw_response)
        _HTTP_HELPERS.log_response_budget(
            response,
            role,
            round_index,
            0.0 if started is None else time.perf_counter() - started,
        )
        observation.record_response(
            http_status=raw_response.status_code,
            usage_factory=lambda: _HTTP_HELPERS.canonical_usage(response),
            response_model_factory=lambda: _HTTP_HELPERS.response_model(response),
        )
        return response


def _protocol_error(
    reason: str,
    role: Role,
    *,
    error_type: str = "invalid_response",
    fingerprint_text: str | None = None,
) -> DeepSeekLlmClientError:
    fingerprint = f" {text_fingerprint(fingerprint_text)}" if fingerprint_text is not None else ""
    _LOG.warning(
        "DeepSeek response rejected role=%s error_type=%s%s",
        role,
        error_type,
        fingerprint,
    )
    return DeepSeekLlmClientError(f"DeepSeekLlmClient: {reason} (role={role})")


def _extract_choice(
    response: ParsedJsonResponse, role: Role
) -> tuple[dict[str, Any], dict[str, Any]]:
    if response.json_error:
        raise _protocol_error("response is not JSON", role)
    body = response.body
    if not isinstance(body, dict):
        raise _protocol_error("response root is not object", role)
    choices = body.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    if not isinstance(choice, dict):
        raise _protocol_error("response has no valid choice", role)
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise _protocol_error("response has no assistant message", role)
    return choice, message


def _content(message: dict[str, Any], role: Role) -> str:
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise _protocol_error(
            "response has no non-empty content",
            role,
            error_type="empty_content",
            fingerprint_text=content if isinstance(content, str) else None,
        )
    return content


def _required_string(value: object, field: str, role: Role) -> str:
    if not isinstance(value, str) or not value:
        raise _protocol_error(f"invalid {field}", role)
    return value


def _parse_tool_calls(message: dict[str, Any], role: Role) -> tuple[list[ToolCall], dict[str, Any]]:
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list) or not raw_calls:
        raise _protocol_error("tool_calls finish without calls", role)
    calls: list[ToolCall] = []
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict) or raw_call.get("type") != "function":
            raise _protocol_error("invalid tool call", role)
        function = raw_call.get("function")
        if not isinstance(function, dict):
            raise _protocol_error("invalid tool function", role)
        call_id = _required_string(raw_call.get("id"), "tool call id", role)
        name = _required_string(function.get("name"), "tool name", role)
        arguments = _required_string(function.get("arguments"), "tool arguments", role)
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise _protocol_error("tool arguments are not JSON", role) from exc
        if not isinstance(args, dict):
            raise _protocol_error("tool arguments root is not object", role)
        calls.append(ToolCall(name=name, args=args, call_id=call_id))
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise _protocol_error("invalid assistant content", role)
    return calls, message


def _finish_label(value: object) -> str:
    if isinstance(value, str) and value in _KNOWN_FINISH_REASONS:
        return value
    return type(value).__name__


def _deepseek_function_declaration(tool: ToolDef) -> dict[str, Any]:
    declaration = tool.function_declaration()
    stack = [declaration["parameters"]]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            schema_type = value.get("type")
            if isinstance(schema_type, str) and schema_type in _SCHEMA_TYPE_NAMES:
                value["type"] = schema_type.lower()
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return declaration
