"""AnthropicLlmClient —— 按 token 走 Anthropic Messages API 做单次 JSON 补全。

``provider=anthropic`` 时心的大脑走这条：把 role 对应的 system prompt + 用户 prompt 交给
``POST /v1/messages``，按 **token 计费**（``ANTHROPIC_API_KEY``），区别于 ``claude_code``
（走 ``claude`` CLI、按 claude.ai 订阅计费）。仍是**单次补全**（不是 tick-as-agent，D-2 不破）；
满足 ``LlmClient`` / ``ManagedLlmClient`` 契约，节点零改动。

为什么是 httpx 不是 anthropic SDK
==================================
项目已依赖 httpx（real_client 同款），单测用 ``httpx.MockTransport`` 拦截真网络已成惯例；
单次 JSON 补全只用到 ``/v1/messages`` 一个端点，直接 httpx POST 比引入 anthropic SDK 这个
新依赖更轻、更可测。

请求形态（刻意从简，兼容 user 自选模型）
=======================================
- header：``x-api-key`` + ``anthropic-version: 2023-06-01`` + ``content-type``。
- body：``system`` = role 对应 system prompt（复用 real_client 的 :func:`system_prompt_for`），
  ``messages`` 只一条 user，``max_tokens`` 必填（Messages API 强制）。
- **不发 ``temperature`` / ``thinking``**：二者在推荐的新模型（Opus 4.8/4.7、Fable 5）上会
  400，而心的模型是 user 经 ``llm.model`` 自选的（可能正是这些）；JSON 生成也不需要它们。
  省掉 = 跨所有 Claude 模型都安全。

凭据 / 计费
==========
``ANTHROPIC_API_KEY`` 必填（env），``ANTHROPIC_BASE_URL`` 可选（默认官方端点，便于走代理/网关）。
``model`` 取 ``llm.model``——provider=anthropic 的部署须把它配成 Claude 模型（如
``claude-opus-4-8`` / ``claude-haiku-4-5``）；配错模型名会在调用时 404，错误信息已点明。

失败 fail-fast（与 real_client / claude_code 同哲学：宁可本 tick 失败，不要「假装在想」）：
HTTP 非 200 / ``stop_reason=="refusal"``（安全分类器拒答，content 空）/ 无文本块 / JSON 解析
失败 / 顶层非 dict → ``AnthropicLlmClientError``。
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

import httpx

from kindred.llm.client import LlmClientError
from kindred.llm.real_client import parse_llm_json, system_prompt_for, text_fingerprint

if TYPE_CHECKING:
    from kindred.config import KindredLlmConfig
    from kindred.llm.client import Role

_LOG = logging.getLogger(__name__)

# Messages API 端点 + 固定 anthropic-version（稳定值，见 platform.claude.com）。
_DEFAULT_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_TIMEOUT_S = 60.0
# 单次补全的输出上限（Messages API 必填）。心各 role 的 JSON 都不大，dream.summarize 摘要
# 约定不超 3000 token——4096 兜底偏紧，放到 8192 留足余量（只是上限，按实际输出计费）。
_DEFAULT_MAX_TOKENS = 8192

# env 变量名（凭据）——集中常量，别处别再硬编码字符串。
_ENV_API_KEY = "ANTHROPIC_API_KEY"  # noqa: S105 - env var name, not a secret value
_ENV_BASE_URL = "ANTHROPIC_BASE_URL"


class AnthropicLlmClientError(LlmClientError):
    """Anthropic Messages API 调用失败（HTTP 非 200 / 拒答 / 无内容 / JSON 解析失败）。

    继承 ``LlmClientError``——daemon / CLI 一行 catch 基类兜底，节点端包装为
    ``SenseLlmContractError`` / ``ActLlmContractError``。fail-fast。
    """


class AnthropicLlmClient:
    """满足 ``ManagedLlmClient`` 契约的 Anthropic Messages API 客户端（按 token 计费）。

    Parameters
    ----------
    api_key
        ``x-api-key``（``ANTHROPIC_API_KEY``）。**永不进日志**。
    model
        ``model`` 字段，须是 Claude 模型（取 ``llm.model``）。
    base_url
        API 基址，默认官方端点；可经 ``ANTHROPIC_BASE_URL`` 改（代理/网关）。
    max_tokens / timeout_s
        单次输出上限（Messages API 必填）/ HTTP 超时。
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
    ) -> AnthropicLlmClient:
        """从 kindred config + env 装配。``ANTHROPIC_API_KEY`` 必填；``ANTHROPIC_BASE_URL`` 可选。

        ``model`` 取 ``llm.model``（config 单一真相源）——provider=anthropic 须配 Claude 模型。

        Raises:
            AnthropicLlmClientError: 缺 ``ANTHROPIC_API_KEY``。
        """
        api_key = os.environ.get(_ENV_API_KEY, "")
        if not api_key:
            msg = f"AnthropicLlmClient: 缺 env 变量 {_ENV_API_KEY}"
            raise AnthropicLlmClientError(msg)
        base_url = os.environ.get(_ENV_BASE_URL) or _DEFAULT_BASE_URL
        return cls(api_key=api_key, model=llm.model, base_url=base_url, timeout_s=timeout_s)

    # ── LlmClient / ManagedLlmClient 契约 ──────────────────────

    def complete(self, prompt: str, *, role: Role) -> dict[str, Any]:
        """调 Messages API 生成内容，解析首个 text 块为 dict。schema 由调用节点验证。"""
        url = f"{self._base_url}/v1/messages"
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system_prompt_for(role),
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        try:
            resp = self._client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            msg = f"AnthropicLlmClient: HTTP 请求失败 (role={role}, model={self._model})：{exc}"
            raise AnthropicLlmClientError(msg) from exc

        if resp.status_code != httpx.codes.OK:
            # 不回显响应体（可能含模型/凭据相关诊断），只记状态码 + 字节数。
            msg = (
                f"AnthropicLlmClient: HTTP {resp.status_code} (role={role}, "
                f"model={self._model}, response_bytes={len(resp.content)})"
            )
            raise AnthropicLlmClientError(msg)

        text = self._extract_text(resp, role=role)
        parsed = self._parse_json(text, role=role)
        _LOG.debug(
            "AnthropicLlmClient ok role=%s model=%s usage=%s",
            role,
            self._model,
            self._safe_usage(resp),
        )
        return parsed

    def close(self) -> None:
        """关闭底层 httpx client（daemon 退出时调）。"""
        self._client.close()

    # ── 私有 helper ────────────────────────────────────────────

    def _extract_text(self, resp: httpx.Response, *, role: Role) -> str:
        """取 Messages API 响应的首个 text 块；先挡拒答（stop_reason=refusal）。"""
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            msg = f"AnthropicLlmClient: 响应非 JSON (role={role})：{exc}"
            raise AnthropicLlmClientError(msg) from exc
        if not isinstance(body, dict):
            msg = f"AnthropicLlmClient: 响应顶层非 dict (role={role})"
            raise AnthropicLlmClientError(msg)
        if body.get("stop_reason") == "refusal":
            # 安全分类器拒答（HTTP 200 但 content 空/无效）；本 tick fail-fast。
            msg = f"AnthropicLlmClient: 模型拒答 stop_reason=refusal (role={role})"
            raise AnthropicLlmClientError(msg)

        content = body.get("content")
        text = None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    candidate = block.get("text")
                    if isinstance(candidate, str) and candidate.strip():
                        text = candidate
                        break
        if text is None:
            msg = f"AnthropicLlmClient: 响应无非空 text 块 (role={role})"
            raise AnthropicLlmClientError(msg)
        return text

    def _parse_json(self, text: str, *, role: Role) -> dict[str, Any]:
        """文本 → dict，容忍散文前言/后语与 markdown 围栏（复用 real_client 的宽松解析）。

        sonnet 在情感活动上常在 JSON 前加一句自述（+ ```json 围栏），:func:`parse_llm_json`
        会先严格 parse、失败再按花括号配对救回。真不可救时异常只带**不含正文的指纹**
        （role + decode 位置 + bytes + sha256），不带模型原文——避免回显进常态日志
        （见 text_fingerprint 与 earlier review review N-1）；全文排查走 gated PromptDumper。
        """
        try:
            parsed = parse_llm_json(text)
        except json.JSONDecodeError as exc:
            msg = (
                f"AnthropicLlmClient: 未返回合法 JSON (role={role})："
                f"{exc}; {text_fingerprint(text)}"
            )
            # raw_text out-of-band 供 gated PromptDumper 落 0600 工件；不进 msg/日志。
            raise AnthropicLlmClientError(msg, raw_text=text) from exc
        if not isinstance(parsed, dict):
            msg = f"AnthropicLlmClient: 返回顶层非 dict (role={role})，got {type(parsed).__name__}"
            raise AnthropicLlmClientError(msg)
        return parsed

    @staticmethod
    def _safe_usage(resp: httpx.Response) -> Any:
        """从响应体取 usage 供 debug 日志；任何异常吞掉返 None（日志不该影响主流程）。"""
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError):
            return None
        return body.get("usage") if isinstance(body, dict) else None


__all__ = ["AnthropicLlmClient", "AnthropicLlmClientError"]
