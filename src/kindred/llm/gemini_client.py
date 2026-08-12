"""GeminiLlmClient —— 按 token 走 Google Generative Language API 做单次 JSON 补全。

``provider=google`` 时心的大脑走这条：把 role 对应的 system prompt + 用户 prompt 交给
``POST /v1beta/models/{model}:generateContent``。区别于 ``anthropic``（Anthropic Messages
API）/ ``claude_code``（claude CLI 订阅）。仍是**单次补全**
（不是 tick-as-agent，D-2 不破）；满足 ``LlmClient`` / ``ManagedLlmClient`` 契约，节点零改动。

为什么加这条
============
Google AI Studio 提供可直接配置的 Gemini API key，适合低成本常驻 tick。

为什么是 httpx 不是 google SDK
==============================
与 :mod:`kindred.llm.anthropic_client` 同哲学：项目已依赖 httpx，单次 JSON 补全只用
``:generateContent`` 一个端点，直接 httpx POST 比引入 ``google-genai`` 新依赖更轻、更可测
（单测用 ``httpx.MockTransport`` 拦截真网络）。

请求形态
========
- header：``x-goog-api-key`` + ``content-type``。
- body：``systemInstruction`` = role 对应 system prompt（复用 real_client 的
  :func:`system_prompt_for`），``contents`` 只一条 user，``generationConfig.maxOutputTokens``，
  并设 ``responseMimeType="application/json"`` —— gemini 据此**强制只输出 JSON**，大幅降低
  「散文前言挤掉 JSON」的概率（仍由共享的 :func:`parse_llm_json` 兜底容忍前言/围栏）。
- **不发 temperature / thinking**：JSON 生成不需要，省掉跨模型都安全。

凭据 / 计费
==========
``GEMINI_API_KEY``（或回退 ``GOOGLE_API_KEY``）必填（env），``GEMINI_BASE_URL`` 可选
（默认官方端点，便于走代理/网关）。``model`` 取 ``llm.model``——provider=google 的部署须
把它配成 gemini 模型（如 ``gemini-3.5-flash``）。

失败 fail-fast（与 anthropic_client 同哲学：宁可本 tick 失败，不要「假装在想」）：
HTTP 非 200 / prompt 被安全闸拦（``promptFeedback.blockReason``）/ candidate 非正常收尾
（``finishReason`` 不是缺失或 ``STOP``）/ 无文本 / JSON 解析失败 / 顶层非 dict →
``GeminiLlmClientError``。
解析失败的异常只带**不含正文的指纹**（role + decode 位置 + bytes + sha256），原文 out-of-band
挂在 ``raw_text`` 上供 gated PromptDumper 落 0600（与三个兄弟 client 一致，见 earlier review N-1）。
"""

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
from kindred.llm.schemas import SenseResponse
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

# Generative Language API 端点基址（官方）。完整路径在 complete() 按 model 拼。
_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
_DEFAULT_TIMEOUT_S = 60.0
# 单次补全输出上限。心各 role 的 JSON 都不大；与 anthropic_client 取同值留足余量。
_DEFAULT_MAX_TOKENS = 8192

# env 变量名（凭据）。GEMINI_API_KEY 优先，回退 GOOGLE_API_KEY（google SDK 习惯）。
_ENV_API_KEY = "GEMINI_API_KEY"  # noqa: S105 - env var name, not a secret value
_ENV_API_KEY_FALLBACK = "GOOGLE_API_KEY"  # noqa: S105 - env var name, not a secret value
_ENV_BASE_URL = "GEMINI_BASE_URL"

# candidate.finishReason 的**唯一正常**值。allowlist 而非 denylist（earlier review review N-1）：
# 只接受缺失（兼容省略该字段的响应）或 STOP；任何其他字符串（SAFETY / RECITATION /
# MAX_TOKENS 截断 / 未来 API 新增枚举…）都视作非正常收尾，内容不可信 → fail-fast。
# denylist 会漏掉没列进集合的新枚举，把非正常 candidate 当成功响应解析。
_OK_FINISH_REASON = "STOP"

_USAGE_FIELDS = {
    "promptTokenCount": "prompt_tokens",
    "cachedContentTokenCount": "cached_tokens",
    "thoughtsTokenCount": "thinking_tokens",
    "candidatesTokenCount": "candidates_tokens",
    "totalTokenCount": "total_tokens",
}


def _sense_response_json_schema() -> dict[str, Any]:
    """从 SenseResponse 派生 Gemini sense 专用的窄 JSON Schema。"""
    source = SenseResponse.model_json_schema()
    properties = source["properties"]
    for field in properties.values():
        field.pop("title", None)
        field.pop("default", None)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": source["required"],
    }
    if "$defs" in source:
        schema["$defs"] = source["$defs"]
    return schema


class GeminiLlmClientError(LlmClientError):
    """Google Generative Language API 调用失败（HTTP 非 200 / 被拦 / 无内容 / 解析失败）。

    继承 ``LlmClientError``——daemon / CLI 一行 catch 基类兜底，节点端包装为
    ``SenseLlmContractError`` / ``ActLlmContractError``。fail-fast。
    """


class GeminiLlmClient:
    """满足 ``ManagedLlmClient`` 契约的 Google Generative Language API 客户端。

    Parameters
    ----------
    api_key
        ``x-goog-api-key``（``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``）。**永不进日志**。
    model
        ``model`` 字段，须是 gemini 模型（取 ``llm.model``，如 ``gemini-3.5-flash``）。
    base_url
        API 基址，默认官方端点；可经 ``GEMINI_BASE_URL`` 改（代理/网关）。
    max_tokens / timeout_s
        单次输出上限 / HTTP 超时。
    transport
        注入点：测试传 ``httpx.MockTransport`` 拦截真网络。
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = _DEFAULT_BASE_URL,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._max_tokens = max_tokens
        self._timeout_s = timeout_s
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    @classmethod
    def from_config(
        cls,
        llm: KindredLlmConfig,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> GeminiLlmClient:
        """从 kindred config + env 装配。``GEMINI_API_KEY``（或 ``GOOGLE_API_KEY``）必填。

        ``model`` 取 ``llm.model``（config 单一真相源）——provider=google 须配 gemini 模型。

        Raises:
            GeminiLlmClientError: 缺 API key env。
        """
        api_key = os.environ.get(_ENV_API_KEY) or os.environ.get(_ENV_API_KEY_FALLBACK) or ""
        if not api_key:
            msg = f"GeminiLlmClient: 缺 env 变量 {_ENV_API_KEY}（或 {_ENV_API_KEY_FALLBACK}）"
            raise GeminiLlmClientError(msg)
        base_url = os.environ.get(_ENV_BASE_URL) or _DEFAULT_BASE_URL
        return cls(api_key=api_key, model=llm.model, base_url=base_url, timeout_s=timeout_s)

    # ── LlmClient / ManagedLlmClient 契约 ──────────────────────

    def complete(self, prompt: str, *, role: Role) -> dict[str, Any]:
        """调 generateContent 生成内容，解析文本为 dict。schema 由调用节点验证。"""
        url = f"{self._base_url}/v1beta/models/{self._model}:generateContent"
        generation_config: dict[str, Any] = {
            "responseMimeType": "application/json",
            "maxOutputTokens": self._max_tokens,
        }
        if role == "sense.llm":
            generation_config["responseJsonSchema"] = _sense_response_json_schema()
        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system_prompt_for(role)}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
        headers = {
            "x-goog-api-key": self._api_key,
            "content-type": "application/json",
        }

        _log_request_budget(payload, role, 1, 1, ())
        started = time.perf_counter()
        resp: httpx.Response | None = None
        transport_error_type: str | None = None
        try:
            resp = self._client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            _log_response_budget(None, role, 1, time.perf_counter() - started)
            transport_error_type = type(exc).__name__
        if resp is None:
            assert transport_error_type is not None
            msg = (
                f"GeminiLlmClient: HTTP 请求失败 (role={role}, model={self._model}, "
                f"error_type={transport_error_type})"
            )
            raise GeminiLlmClientError(msg)

        _log_response_budget(resp, role, 1, time.perf_counter() - started)
        if resp.status_code != httpx.codes.OK:
            # 不回显响应体（可能含模型/凭据相关诊断），只记状态码 + 字节数。
            msg = (
                f"GeminiLlmClient: HTTP {resp.status_code} (role={role}, "
                f"model={self._model}, response_bytes={len(resp.content)})"
            )
            raise GeminiLlmClientError(msg)

        text = self._extract_text(resp, role=role)
        parsed = self._parse_json(text, role=role)
        return parsed

    def close(self) -> None:
        """关闭底层 httpx client（daemon 退出时调）。"""
        self._client.close()

    # ── ToolCapableLlmClient 契约 ───────────────────────────────

    def complete_with_tools(
        self,
        prompt: str,
        *,
        role: Role,
        tools: Sequence[ToolDef],
        handler: ToolHandler,
        max_rounds: int,
    ) -> ToolLoopResult:
        """调 Gemini 有界工具环，末轮文本用 ``parse_llm_json`` 解析为 final dict。

        M0 探针结论：
        - 工具环本身可走通；
        - ``ANY`` 强制工具调用 + ``responseMimeType=application/json`` 会被 Gemini 拒绝；
        - 因此 tool-loop 路径**不设置 responseMimeType**，末轮靠 prompt 约束 +
          ``parse_llm_json`` 兜底。

        Gemini 还要求 functionCall 历史里保留 provider 返回的内部
        ``thoughtSignature``/``thought_signature`` 等字段。实现上必须把模型返回的
        candidate ``content`` 原样 append 回 ``contents``，不能从 ``ToolCall`` 重建。
        """

        if max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")

        known_tools = tool_map(tuple(tools))
        url = f"{self._base_url}/v1beta/models/{self._model}:generateContent"
        contents: list[dict[str, Any]] = [{"role": "user", "parts": [{"text": prompt}]}]
        events: list[ToolEvent] = []

        for round_index in range(1, max_rounds + 1):
            payload = self._tool_payload(role=role, contents=contents, tools=tools)
            _log_request_budget(payload, role, round_index, max_rounds, tools)
            started = time.perf_counter()
            resp: httpx.Response | None = None
            transport_error_type: str | None = None
            try:
                resp = self._client.post(
                    url,
                    json=payload,
                    headers={
                        "x-goog-api-key": self._api_key,
                        "content-type": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                _log_response_budget(None, role, round_index, time.perf_counter() - started)
                transport_error_type = type(exc).__name__
            if resp is None:
                assert transport_error_type is not None
                msg = (
                    "GeminiLlmClient: tool-loop HTTP 请求失败 "
                    f"(role={role}, error_type={transport_error_type})"
                )
                self._raise_or_wrap_tool_loop(msg, events=events, rounds=round_index - 1)

            _log_response_budget(resp, role, round_index, time.perf_counter() - started)
            if resp.status_code != httpx.codes.OK:
                msg = (
                    f"GeminiLlmClient: tool-loop HTTP {resp.status_code} "
                    f"(role={role}, model={self._model}, response_bytes={len(resp.content)})"
                )
                self._raise_or_wrap_tool_loop(msg, events=events, rounds=round_index - 1)

            try:
                content, parts = self._extract_tool_content(resp, role=role)
            except GeminiLlmClientError as exc:
                self._raise_or_wrap_tool_loop(
                    str(exc),
                    events=events,
                    rounds=round_index - 1,
                    raw_text=getattr(exc, "raw_text", None),
                )

            calls = self._extract_tool_calls(parts)
            if calls:
                # 原样回放模型 content：Gemini functionCall parts 可能携带 thoughtSignature。
                contents.append(content)
                response_parts: list[dict[str, Any]] = []
                for call in calls:
                    result = self._tool_result_for_call(call, handler, known_tools)
                    events.append(
                        ToolEvent(
                            round_index=round_index,
                            call=call,
                            result=result,
                            effect=effect_for_tool(known_tools, call.name),
                        )
                    )
                    response_parts.append(self._function_response_part(call, result))
                contents.append({"role": "user", "parts": response_parts})
                continue

            text = self._parts_text(parts)
            if not text.strip():
                msg = f"GeminiLlmClient: tool-loop final 响应无非空文本 (role={role})"
                raise ToolLoopError(msg, tool_events=events, rounds=round_index)
            try:
                final = self._parse_json(text, role=role)
            except GeminiLlmClientError as exc:
                msg = f"GeminiLlmClient: tool-loop final 非合法 JSON (role={role})：{exc}"
                raise ToolLoopError(
                    msg,
                    tool_events=events,
                    rounds=round_index,
                    raw_text=getattr(exc, "raw_text", None),
                ) from exc
            return ToolLoopResult(final=final, tool_events=tuple(events), rounds=round_index)

        msg = f"GeminiLlmClient: tool-loop 达到 max_rounds={max_rounds} 仍无 final"
        raise ToolLoopError(msg, tool_events=events, rounds=max_rounds)

    # ── 私有 helper ────────────────────────────────────────────

    def _tool_payload(
        self,
        *,
        role: Role,
        contents: list[dict[str, Any]],
        tools: Sequence[ToolDef],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "systemInstruction": {
                "parts": [
                    {
                        "text": act_tool_loop_system_prompt_for(tools)
                        if role == "act.llm"
                        else system_prompt_for(role)
                    }
                ]
            },
            "contents": contents,
            "generationConfig": {
                # M0: tool-loop path intentionally omits responseMimeType.
                "maxOutputTokens": self._max_tokens,
            },
        }
        if tools:
            payload["tools"] = [
                {"functionDeclarations": [_gemini_function_declaration(tool) for tool in tools]}
            ]
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
        return payload

    def _extract_text(self, resp: httpx.Response, *, role: Role) -> str:
        """取 generateContent 响应里 candidate 的文本；先挡 prompt 拦截 / 异常 finishReason。"""
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            msg = f"GeminiLlmClient: 响应非 JSON (role={role})：{exc}"
            raise GeminiLlmClientError(msg) from exc
        if not isinstance(body, dict):
            msg = f"GeminiLlmClient: 响应顶层非 dict (role={role})"
            raise GeminiLlmClientError(msg)

        # prompt 级安全拦截：promptFeedback.blockReason（candidates 多半空）。
        feedback = body.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            reason = feedback.get("blockReason")
            msg = f"GeminiLlmClient: prompt 被安全闸拦 blockReason={reason} (role={role})"
            raise GeminiLlmClientError(msg)

        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            msg = f"GeminiLlmClient: 响应无 candidates (role={role})"
            raise GeminiLlmClientError(msg)
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            msg = f"GeminiLlmClient: candidate 非 dict (role={role})"
            raise GeminiLlmClientError(msg)

        # candidate 级 finishReason：allowlist——只接受缺失或 STOP，其余一律 fail-fast
        # denylist 会漏掉未列入的新枚举，把非正常 candidate 当作成功响应解析。
        finish = candidate.get("finishReason")
        if isinstance(finish, str) and finish != _OK_FINISH_REASON:
            msg = (
                f"GeminiLlmClient: candidate 非正常收尾 finishReason={finish}"
                f"（仅接受缺失或 {_OK_FINISH_REASON}）(role={role})"
            )
            raise GeminiLlmClientError(msg)

        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        text = ""
        if isinstance(parts, list):
            # 拼接所有 text part（responseMimeType=json 时一般只一段）。
            text = "".join(
                p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)
            )
        if not text.strip():
            msg = f"GeminiLlmClient: 响应无非空文本 (role={role})"
            raise GeminiLlmClientError(msg)
        return text

    def _extract_tool_content(
        self,
        resp: httpx.Response,
        *,
        role: Role,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """取 tool-loop candidate content 与 parts；保留 content 原样供历史回放。"""

        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            msg = f"GeminiLlmClient: tool-loop 响应非 JSON (role={role})：{exc}"
            raise GeminiLlmClientError(msg) from exc
        if not isinstance(body, dict):
            msg = f"GeminiLlmClient: tool-loop 响应顶层非 dict (role={role})"
            raise GeminiLlmClientError(msg)

        feedback = body.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            reason = feedback.get("blockReason")
            msg = f"GeminiLlmClient: tool-loop prompt 被安全闸拦 blockReason={reason} (role={role})"
            raise GeminiLlmClientError(msg)

        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            msg = f"GeminiLlmClient: tool-loop 响应无 candidates (role={role})"
            raise GeminiLlmClientError(msg)
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            msg = f"GeminiLlmClient: tool-loop candidate 非 dict (role={role})"
            raise GeminiLlmClientError(msg)

        finish = candidate.get("finishReason")
        if isinstance(finish, str) and finish != _OK_FINISH_REASON:
            msg = (
                f"GeminiLlmClient: tool-loop candidate 非正常收尾 finishReason={finish}"
                f"（仅接受缺失或 {_OK_FINISH_REASON}）(role={role})"
            )
            raise GeminiLlmClientError(msg)

        content = candidate.get("content")
        if not isinstance(content, dict):
            msg = f"GeminiLlmClient: tool-loop candidate.content 非 dict (role={role})"
            raise GeminiLlmClientError(msg)
        raw_parts = content.get("parts")
        if not isinstance(raw_parts, list) or not raw_parts:
            msg = f"GeminiLlmClient: tool-loop 响应无 parts (role={role})"
            raise GeminiLlmClientError(msg)
        parts = [part for part in raw_parts if isinstance(part, dict)]
        if not parts:
            msg = f"GeminiLlmClient: tool-loop parts 无 dict part (role={role})"
            raise GeminiLlmClientError(msg)
        return content, parts

    @staticmethod
    def _extract_tool_calls(parts: list[dict[str, Any]]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for part in parts:
            raw_call = part.get("functionCall")
            if not isinstance(raw_call, dict):
                continue
            raw_name = raw_call.get("name")
            raw_args = raw_call.get("args")
            raw_id = raw_call.get("id")
            calls.append(
                ToolCall(
                    name=raw_name if isinstance(raw_name, str) else "",
                    args=raw_args if isinstance(raw_args, dict) else {},
                    call_id=raw_id if isinstance(raw_id, str) else None,
                )
            )
        return calls

    @staticmethod
    def _parts_text(parts: list[dict[str, Any]]) -> str:
        return "".join(part["text"] for part in parts if isinstance(part.get("text"), str))

    @staticmethod
    def _tool_result_for_call(
        call: ToolCall,
        handler: ToolHandler,
        known_tools: dict[str, ToolDef],
    ) -> ToolResult:
        if call.name not in known_tools:
            return ToolResult.error(
                call,
                error_type="UnknownTool",
                message="tool is not registered",
            )
        return call_tool_handler(handler, call)

    @staticmethod
    def _function_response_part(call: ToolCall, result: ToolResult) -> dict[str, Any]:
        function_response: dict[str, Any] = {
            "name": result.name,
            "response": result.response,
        }
        call_id = call.call_id
        if call_id:
            function_response["id"] = call_id
        return {"functionResponse": function_response}

    @staticmethod
    def _raise_or_wrap_tool_loop(
        message: str,
        *,
        events: list[ToolEvent],
        rounds: int,
        raw_text: str | None = None,
    ) -> NoReturn:
        if events:
            raise ToolLoopError(
                message,
                tool_events=events,
                rounds=rounds,
                raw_text=raw_text,
            )
        raise GeminiLlmClientError(message, raw_text=raw_text)

    def _parse_json(self, text: str, *, role: Role) -> dict[str, Any]:
        """文本 → dict，容忍散文前言/后语与 markdown 围栏（复用 real_client 的宽松解析）。

        responseMimeType=json 已让 gemini 大概率只吐 JSON；:func:`parse_llm_json` 再兜一层
        （先严格 parse、失败按花括号配对救回）。真不可救时异常只带**不含正文的指纹**，原文
        挂 ``raw_text`` 供 gated PromptDumper 落 0600（见 text_fingerprint 与 earlier review N-1）。
        """
        try:
            parsed = parse_llm_json(text)
        except json.JSONDecodeError as exc:
            msg = f"GeminiLlmClient: 未返回合法 JSON (role={role})：{exc}; {text_fingerprint(text)}"
            raise GeminiLlmClientError(msg, raw_text=text) from exc
        if not isinstance(parsed, dict):
            msg = f"GeminiLlmClient: 返回顶层非 dict (role={role})，got {type(parsed).__name__}"
            raise GeminiLlmClientError(msg)
        return parsed


def _gemini_function_declaration(tool: ToolDef) -> dict[str, Any]:
    """投影到 Gemini legacy function declaration 支持的参数子集。"""
    declaration = tool.function_declaration()
    parameters = declaration.get("parameters")
    if isinstance(parameters, dict):
        parameters.pop("additionalProperties", None)
    return declaration


def _part_text(value: object) -> str:
    parts = value.get("parts") if isinstance(value, dict) else None
    first = parts[0] if isinstance(parts, list) and parts else None
    text = first.get("text") if isinstance(first, dict) else None
    return text if isinstance(text, str) else ""


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_chars(value: object) -> int:
    return len(_compact_json(value)) if value else 0


def _log_request_budget(
    payload: dict[str, Any],
    role: Role,
    round_index: int,
    max_rounds: int,
    tools: Sequence[ToolDef],
) -> None:
    if not _LOG.isEnabledFor(logging.DEBUG):
        return
    contents = payload["contents"]
    history = [item for item in contents[1:] if item.get("role") != "user"]
    results = [
        part["functionResponse"]
        for item in contents[1:]
        for part in item.get("parts", [])
        if isinstance(part.get("functionResponse"), dict)
    ]
    config = payload["generationConfig"]
    budget = {
        "role": role,
        "round": round_index,
        "max_rounds": max_rounds,
        "payload_json_bytes": len(_compact_json(payload).encode()),
        "system_text_chars": len(_part_text(payload["systemInstruction"])),
        "initial_user_text_chars": len(_part_text(contents[0])),
        "tool_schema_json_chars": _json_chars(payload.get("tools")),
        "response_schema_json_chars": _json_chars(config.get("responseJsonSchema")),
        "model_history_json_chars": _json_chars(history),
        "tool_result_json_chars": _json_chars(results),
        "tool_names": tuple(tool.name for tool in tools),
    }
    _LOG.debug("Gemini request_budget %s", budget, extra={"kindred_prompt_budget": budget})


def _log_response_budget(
    resp: httpx.Response | None, role: Role, round_index: int, elapsed_s: float
) -> None:
    if not _LOG.isEnabledFor(logging.DEBUG):
        return
    usage: dict[str, int] = {}
    if resp is not None:
        try:
            raw = resp.json().get("usageMetadata", {})
        except (AttributeError, ValueError):
            raw = {}
        raw = raw if isinstance(raw, dict) else {}
        usage = {
            safe: value
            for provider, safe in _USAGE_FIELDS.items()
            if isinstance((value := raw.get(provider)), int) and not isinstance(value, bool)
        }
    response = {
        "role": role,
        "round": round_index,
        "status_code": resp.status_code if resp is not None else None,
        "elapsed_ms": round(elapsed_s * 1000),
        **usage,
    }
    _LOG.debug("Gemini response_budget %s", response, extra={"kindred_response_budget": response})


__all__ = ["GeminiLlmClient", "GeminiLlmClientError"]
