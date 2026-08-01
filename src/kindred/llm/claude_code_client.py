"""ClaudeCodeLlmClient —— 走 ``claude`` CLI headless 做单次 JSON 补全（earlier milestone 路 a）。

``provider=claude_code`` 时心的大脑走这条：把 role 对应的 system prompt + 用户 prompt
交给 ``claude -p --output-format json``，按 **claude.ai 订阅**计费（登录态），而非按
token 的 Messages API。仍是**单次补全**（不是 tick-as-agent，D-2 不破）；满足
``LlmClient`` / ``ManagedLlmClient`` 契约，节点零改动。

为什么走 CLI 子进程而非 Agent SDK
================================
心 daemon 是**同步**的（节点同步调 ``complete``）。CLI 子进程天然同步、auth 自动继承
机器上的 ``claude`` 登录态、可注入 fake runner 离线单测——比把 async Agent SDK 桥成
同步更简单。

凭据 / 计费
==========
**不加** ``--bare``（``--bare`` 会强制 ``ANTHROPIC_API_KEY``，绕开订阅）。靠机器上
``claude auth login`` / ``claude setup-token`` 的订阅登录态。部署机需登录一次。
``--system-prompt`` 整体替换默认 system prompt + ``--tools ""`` 关掉所有工具 + 在中立
cwd 跑（避开项目 CLAUDE.md），把上下文收成「我的 system + 用户 prompt」单次问答。

输出信封
========
``--output-format json`` 返回一层信封 ``{type, subtype, is_error, result, total_cost_usd,
usage, ...}``；``result`` 字段是模型文本（即我们要的 JSON）。解析路径：
信封 JSON → 取 ``result`` → 去 markdown 围栏 → ``json.loads`` → dict。
``is_error=true`` / 非 0 退出 / 解析失败 → ``ClaudeCodeLlmClientError``（fail-fast，与
其他 Provider client 同哲学：宁可本 tick 失败，不要「假装在想」污染状态）。
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any, Protocol

from kindred.llm.client import LlmClientError
from kindred.llm.real_client import parse_llm_json, system_prompt_for, text_fingerprint

if TYPE_CHECKING:
    from kindred.config import KindredLlmConfig
    from kindred.llm.client import Role

_LOG = logging.getLogger(__name__)

_DEFAULT_CLAUDE_BIN = "claude"
_DEFAULT_TIMEOUT_S = 120.0


class ClaudeCodeLlmClientError(LlmClientError):
    """claude-code 调用失败（子进程失败 / 信封 is_error / 非 0 退出 / JSON 解析失败）。

    继承 ``LlmClientError``（earlier review 二轮 N-1）——daemon / CLI 一行 catch 基类兜底，
    节点端则包装为 ``SenseLlmContractError`` / ``ActLlmContractError``。fail-fast。
    """


class _CompletedLike(Protocol):
    """``subprocess.run`` 返回值（text 模式）的最小契约——便于测试注入 fake。"""

    returncode: int
    stdout: str
    stderr: str


class _Runner(Protocol):
    """跑 ``claude`` 子进程的可注入入口：``(argv, prompt, timeout, cwd) -> CompletedLike``。"""

    def __call__(
        self, argv: list[str], prompt: str, timeout: float, cwd: str
    ) -> _CompletedLike: ...


def _default_runner(
    argv: list[str], prompt: str, timeout: float, cwd: str
) -> subprocess.CompletedProcess[str]:
    """真子进程：prompt 经 stdin 喂入（避免 argv 长度/转义问题），在中立 ``cwd`` 跑取 stdout。"""
    return subprocess.run(  # noqa: S603 - argv 全为本模块构造的常量 + 受控 model/prompt
        argv,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        check=False,
    )


class ClaudeCodeLlmClient:
    """满足 ``ManagedLlmClient`` 契约的 claude-code headless 客户端。

    Parameters
    ----------
    model
        传给 ``--model`` 的模型（Claude 别名如 ``haiku`` / ``sonnet`` 或全名）。取自
        ``config.llm.model``——provider=claude_code 的部署应把 model 配成 Claude 模型。
    claude_bin
        ``claude`` 可执行文件（默认走 PATH 上的 ``claude``）。
    runner
        子进程入口，可注入 fake 离线测试（默认 :func:`_default_runner`）。
    timeout_s
        单次调用超时秒数。
    cwd
        子进程工作目录。默认系统临时目录——避开项目 CLAUDE.md 被当上下文注入。
    """

    def __init__(
        self,
        *,
        model: str,
        claude_bin: str = _DEFAULT_CLAUDE_BIN,
        runner: _Runner = _default_runner,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        cwd: str | None = None,
    ) -> None:
        self._model = model
        self._claude_bin = claude_bin
        self._runner = runner
        self._timeout_s = timeout_s
        # 中立 cwd：避开项目 CLAUDE.md 自动发现（--system-prompt 只替换 system，
        # CLAUDE.md 仍按 cwd 上溯注入为项目上下文，会污染单次问答）。
        self._cwd = cwd if cwd is not None else tempfile.gettempdir()

    @classmethod
    def from_config(cls, llm: KindredLlmConfig) -> ClaudeCodeLlmClient:
        """从 kindred config 装配（model 取 ``llm.model``、超时取 ``llm.claude_code_timeout_s``）。

        auth 走机器登录态，无凭据入参。超时可配——整机试运行实测：心嘴并发各起 claude
        子进程共用同一订阅时单次心调用会超默认 120s，故放宽（默认 300s，见 config）。
        """
        return cls(model=llm.model, timeout_s=float(llm.claude_code_timeout_s))

    # ── LlmClient / ManagedLlmClient 契约 ──────────────────────

    def complete(self, prompt: str, *, role: Role) -> dict[str, Any]:
        """调 claude-code 生成内容，解析信封 ``result`` 为 dict。schema 由调用节点验证。"""
        argv = [
            self._claude_bin,
            "-p",
            # 不把这次会话（stdin prompt 含 SOUL / 对话窗口 / USER 等敏感内容）写进本机
            # Claude Code session 存储——绕过项目「prompt/debug dump 须显式开启」边界（codex N-2）。
            "--no-session-persistence",
            "--output-format",
            "json",
            "--system-prompt",
            system_prompt_for(role),
            "--model",
            self._model,
            "--tools",
            "",
        ]
        try:
            # cwd=中立目录：真正传给子进程，避开项目 CLAUDE.md 上溯注入（codex N-1）。
            result = self._runner(argv, prompt, self._timeout_s, self._cwd)
        except (OSError, subprocess.SubprocessError) as exc:
            msg = f"ClaudeCodeLlmClient: 子进程失败 (role={role}, model={self._model})：{exc}"
            raise ClaudeCodeLlmClientError(msg) from exc

        if result.returncode != 0:
            msg = (
                f"ClaudeCodeLlmClient: claude 退出码 {result.returncode} "
                f"(role={role}, model={self._model})：{result.stderr[:300]}"
            )
            raise ClaudeCodeLlmClientError(msg)

        envelope = self._parse_envelope(result.stdout, role=role)
        if envelope.get("is_error"):
            msg = (
                f"ClaudeCodeLlmClient: claude 报错 (role={role}, "
                f"subtype={envelope.get('subtype')})：{envelope.get('result')}"
            )
            raise ClaudeCodeLlmClientError(msg)

        text = envelope.get("result")
        if not isinstance(text, str) or not text.strip():
            msg = f"ClaudeCodeLlmClient: 信封 result 为空 (role={role})"
            raise ClaudeCodeLlmClientError(msg)

        parsed = self._parse_json_result(text, role=role)
        _LOG.debug(
            "ClaudeCodeLlmClient ok role=%s model=%s cost_usd=%s",
            role,
            self._model,
            envelope.get("total_cost_usd"),
        )
        return parsed

    def close(self) -> None:
        """no-op：每次调用起独立子进程，无长连接需关闭（满足 ManagedLlmClient）。"""

    # ── 私有 helper ────────────────────────────────────────────

    def _parse_envelope(self, stdout: str, *, role: Role) -> dict[str, Any]:
        try:
            env = json.loads(stdout)
        except json.JSONDecodeError as exc:
            msg = (
                f"ClaudeCodeLlmClient: 信封非 JSON (role={role})："
                f"{exc}; bytes={len(stdout.encode('utf-8'))}"
            )
            raise ClaudeCodeLlmClientError(msg) from exc
        if not isinstance(env, dict):
            msg = f"ClaudeCodeLlmClient: 信封顶层非 dict (role={role})，got {type(env).__name__}"
            raise ClaudeCodeLlmClientError(msg)
        return env

    def _parse_json_result(self, text: str, *, role: Role) -> dict[str, Any]:
        """信封 ``result`` 文本 → dict（容忍散文前言/后语 + markdown 围栏，复用宽松解析）。

        Claude 模型也会在 JSON 前加自述（见 :func:`parse_llm_json`）——先严格 parse、
        失败再按花括号配对救回。真不可救时异常只带**不含正文的指纹**（role + decode
        位置 + bytes + sha256），不带模型原文——避免回显进常态日志（见 text_fingerprint
        与 earlier review review N-1）；全文排查走 gated PromptDumper。
        """
        try:
            parsed = parse_llm_json(text)
        except json.JSONDecodeError as exc:
            msg = (
                f"ClaudeCodeLlmClient: result 非合法 JSON (role={role})："
                f"{exc}; {text_fingerprint(text)}"
            )
            # raw_text out-of-band 供 gated PromptDumper 落 0600 工件；不进 msg/日志。
            raise ClaudeCodeLlmClientError(msg, raw_text=text) from exc
        if not isinstance(parsed, dict):
            msg = (
                f"ClaudeCodeLlmClient: result 顶层非 dict (role={role})，"
                f"got {type(parsed).__name__}"
            )
            raise ClaudeCodeLlmClientError(msg)
        return parsed


__all__ = ["ClaudeCodeLlmClient", "ClaudeCodeLlmClientError"]
