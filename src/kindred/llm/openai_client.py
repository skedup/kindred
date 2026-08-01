"""OpenAI Responses API client with bounded tool-loop support."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, NoReturn

import httpx

from kindred.llm.client import LlmClientError, ToolLoopError
from kindred.llm.real_client import (
    act_tool_loop_system_prompt_for,
    parse_llm_json,
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
)
from kindred_capability_sdk import ToolCall, ToolDef, ToolResult

if TYPE_CHECKING:
    from kindred.config import KindredLlmConfig
    from kindred.llm.client import Role

_LOG = logging.getLogger(__name__)
_SCHEMA_TYPE_NAMES = frozenset({"OBJECT", "STRING", "NUMBER", "INTEGER", "BOOLEAN", "ARRAY"})
_SAFE_INCOMPLETE_REASONS = frozenset({"content_filter", "max_output_tokens"})


class OpenAILlmClientError(LlmClientError):
    pass


class OpenAILlmClient:
    """Direct, non-streaming client for ``POST /v1/responses``."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        max_output_tokens: int = 25_000,
        timeout_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._max_output_tokens = max_output_tokens
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    @classmethod
    def from_config(cls, llm: KindredLlmConfig) -> OpenAILlmClient:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise OpenAILlmClientError("OpenAILlmClient: missing env OPENAI_API_KEY")
        base_url = os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        return cls(api_key=api_key, model=llm.model, base_url=base_url)

    def complete(self, prompt: str, *, role: Role) -> dict[str, Any]:
        input_items = _initial_input(system_prompt_for(role), prompt)
        payload = self._payload(input_items=input_items, tools=())
        response = self._post(payload, role=role, round_index=1, max_rounds=1, tools=())
        output = _response_output(response, role)
        if any(item.get("type") == "function_call" for item in output):
            raise _protocol_error("response contains an unexpected function call", role)
        return _parse_json(_output_text(output, role), role)

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
            try:
                response = self._post(
                    payload,
                    role=role,
                    round_index=round_index,
                    max_rounds=max_rounds,
                    tools=tools,
                )
                output = _response_output(response, role)
                calls = _parse_tool_calls(output, role)
            except OpenAILlmClientError as exc:
                _raise_or_wrap(str(exc), events, round_index - 1, raw_text=exc.raw_text)

            if calls:
                input_items.extend(output)
                for call in calls:
                    result = (
                        call_tool_handler(handler, call)
                        if call.name in known_tools
                        else ToolResult.error(
                            call,
                            error_type="UnknownTool",
                            message="tool is not registered",
                        )
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

            try:
                final = _parse_json(_output_text(output, role), role)
            except OpenAILlmClientError as exc:
                raise ToolLoopError(
                    f"OpenAILlmClient: invalid tool-loop final JSON (role={role})",
                    tool_events=events,
                    rounds=round_index,
                    raw_text=exc.raw_text,
                ) from exc
            return ToolLoopResult(final=final, tool_events=tuple(events), rounds=round_index)

        raise ToolLoopError(
            f"OpenAILlmClient: reached max_rounds={max_rounds} without final",
            tool_events=events,
            rounds=max_rounds,
        )

    def _payload(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: Sequence[ToolDef],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            "text": {
                "format": {"type": "json_object"},
                "verbosity": "low",
            },
            "reasoning": {"effort": "high"},
            "max_output_tokens": self._max_output_tokens,
            "store": False,
            "stream": False,
        }
        if tools:
            payload["tools"] = [_openai_function_declaration(tool) for tool in tools]
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = True
        return payload

    def _post(
        self,
        payload: dict[str, Any],
        *,
        role: Role,
        round_index: int,
        max_rounds: int,
        tools: Sequence[ToolDef],
    ) -> httpx.Response:
        _log_request_budget(payload, role, round_index, max_rounds, tools)
        started = time.perf_counter()
        response: httpx.Response | None = None
        transport_error_type: str | None = None
        try:
            response = self._client.post(
                f"{self._base_url}/responses",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            _log_response_budget(None, role, round_index, time.perf_counter() - started)
            transport_error_type = type(exc).__name__
        if response is None:
            assert transport_error_type is not None
            raise OpenAILlmClientError(
                "OpenAILlmClient: HTTP request failed "
                f"(role={role}, model={self._model}, error_type={transport_error_type})"
            )
        _log_response_budget(response, role, round_index, time.perf_counter() - started)
        if response.status_code != httpx.codes.OK:
            raise OpenAILlmClientError(
                f"OpenAILlmClient: HTTP {response.status_code} "
                f"(role={role}, model={self._model}, response_bytes={len(response.content)})"
            )
        return response


def _initial_input(system: str, prompt: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]


def _protocol_error(
    reason: str,
    role: Role,
    *,
    error_type: str = "invalid_response",
    fingerprint_text: str | None = None,
) -> OpenAILlmClientError:
    fingerprint = f" {text_fingerprint(fingerprint_text)}" if fingerprint_text is not None else ""
    _LOG.warning(
        "OpenAI response rejected role=%s error_type=%s%s",
        role,
        error_type,
        fingerprint,
    )
    return OpenAILlmClientError(f"OpenAILlmClient: {reason} (role={role})")


def _response_output(response: httpx.Response, role: Role) -> list[dict[str, Any]]:
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise _protocol_error("response is not JSON", role) from exc
    if not isinstance(body, dict):
        raise _protocol_error("response root is not object", role)
    status = body.get("status")
    if status != "completed":
        if status == "incomplete":
            details = body.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            if reason in _SAFE_INCOMPLETE_REASONS:
                raise _protocol_error(
                    f"response incomplete ({reason})",
                    role,
                    error_type=f"incomplete_{reason}",
                )
            raise _protocol_error("response is incomplete", role, error_type="incomplete")
        error_type = "failed" if status == "failed" else "invalid_status"
        raise _protocol_error("response status is not completed", role, error_type=error_type)
    output = body.get("output")
    if not isinstance(output, list) or not output:
        raise _protocol_error("response has no output", role)
    if not all(isinstance(item, dict) for item in output):
        raise _protocol_error("response output contains a non-object item", role)
    return output


def _output_text(output: list[dict[str, Any]], role: Role) -> str:
    messages = [item for item in output if item.get("type") == "message"]
    if len(messages) != 1 or messages[0].get("role") != "assistant":
        raise _protocol_error("response has no unique assistant message", role)
    content = messages[0].get("content")
    if not isinstance(content, list) or not content:
        raise _protocol_error("assistant message has no content", role)
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            raise _protocol_error("assistant content contains a non-object item", role)
        if item.get("type") == "refusal":
            raise _protocol_error("assistant refused the request", role, error_type="refusal")
        if item.get("type") != "output_text":
            raise _protocol_error("assistant content has an unsupported type", role)
        text = item.get("text")
        if not isinstance(text, str):
            raise _protocol_error("assistant output text is invalid", role)
        parts.append(text)
    value = "".join(parts)
    if not value.strip():
        raise _protocol_error(
            "response has no non-empty content",
            role,
            error_type="empty_content",
            fingerprint_text=value,
        )
    return value


def _required_string(value: object, field: str, role: Role) -> str:
    if not isinstance(value, str) or not value:
        raise _protocol_error(f"invalid {field}", role)
    return value


def _parse_tool_calls(output: list[dict[str, Any]], role: Role) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for item in output:
        if item.get("type") != "function_call":
            continue
        call_id = _required_string(item.get("call_id"), "tool call id", role)
        name = _required_string(item.get("name"), "tool name", role)
        arguments = _required_string(item.get("arguments"), "tool arguments", role)
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise _protocol_error("tool arguments are not JSON", role) from exc
        if not isinstance(args, dict):
            raise _protocol_error("tool arguments root is not object", role)
        calls.append(ToolCall(name=name, args=args, call_id=call_id))
    return calls


def _raise_or_wrap(
    message: str,
    events: list[ToolEvent],
    rounds: int,
    *,
    raw_text: str | None = None,
) -> NoReturn:
    if events:
        raise ToolLoopError(
            message,
            tool_events=events,
            rounds=rounds,
            raw_text=raw_text,
        )
    raise OpenAILlmClientError(message, raw_text=raw_text)


def _parse_json(text: str, role: Role) -> dict[str, Any]:
    try:
        parsed = parse_llm_json(text)
    except json.JSONDecodeError as exc:
        _LOG.warning(
            "OpenAI response rejected role=%s error_type=invalid_json %s",
            role,
            text_fingerprint(text),
        )
        raise OpenAILlmClientError(
            f"OpenAILlmClient: invalid JSON (role={role}); {text_fingerprint(text)}",
            raw_text=text,
        ) from exc
    if not isinstance(parsed, dict):
        raise _protocol_error(
            f"JSON root is {type(parsed).__name__}, not object",
            role,
            error_type="json_root_not_object",
            fingerprint_text=text,
        )
    return parsed


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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


def _log_request_budget(
    payload: dict[str, Any],
    role: Role,
    round_index: int,
    max_rounds: int,
    tools: Sequence[ToolDef],
) -> None:
    if not _LOG.isEnabledFor(logging.DEBUG):
        return
    input_items = payload["input"]
    history = [item for item in input_items[2:] if item.get("type") != "function_call_output"]
    results = [item for item in input_items[2:] if item.get("type") == "function_call_output"]
    budget = {
        "role": role,
        "round": round_index,
        "max_rounds": max_rounds,
        "payload_json_bytes": len(_compact_json(payload).encode()),
        "system_text_chars": len(input_items[0]["content"]),
        "initial_user_text_chars": len(input_items[1]["content"]),
        "tool_schema_json_chars": len(_compact_json(payload["tools"])) if tools else 0,
        "response_schema_json_chars": 0,
        "model_history_json_chars": len(_compact_json(history)) if history else 0,
        "tool_result_json_chars": len(_compact_json(results)) if results else 0,
        "tool_names": tuple(tool.name for tool in tools),
    }
    _LOG.debug("OpenAI request_budget %s", budget, extra={"kindred_prompt_budget": budget})


def _log_response_budget(
    response: httpx.Response | None,
    role: Role,
    round_index: int,
    elapsed_s: float,
) -> None:
    raw: dict[str, Any] = {}
    if not _LOG.isEnabledFor(logging.DEBUG):
        return
    if response is not None:
        try:
            value = response.json().get("usage", {})
        except (AttributeError, ValueError):
            value = {}
        raw = value if isinstance(value, dict) else {}
    usage = {
        field: value
        for field in ("input_tokens", "output_tokens", "total_tokens")
        if isinstance((value := raw.get(field)), int) and not isinstance(value, bool)
    }
    input_details = raw.get("input_tokens_details")
    cached = input_details.get("cached_tokens") if isinstance(input_details, dict) else None
    if isinstance(cached, int) and not isinstance(cached, bool):
        usage["cached_tokens"] = cached
    output_details = raw.get("output_tokens_details")
    reasoning = output_details.get("reasoning_tokens") if isinstance(output_details, dict) else None
    if isinstance(reasoning, int) and not isinstance(reasoning, bool):
        usage["reasoning_tokens"] = reasoning
    budget = {
        "role": role,
        "round": round_index,
        "status_code": response.status_code if response is not None else None,
        "elapsed_ms": round(elapsed_s * 1000),
        **usage,
    }
    _LOG.debug("OpenAI response_budget %s", budget, extra={"kindred_response_budget": budget})


__all__ = ["OpenAILlmClient", "OpenAILlmClientError"]
