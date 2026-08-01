"""Kindred 心侧可观测：进程日志 + 受闸控的逐 tick 调试 dump。

本文件就是 Kindred 自己的「可观测」层——心 daemon 的 :func:`configure_logging`
（日志）+ 受 ``debug.dump_enabled`` 闸控的 :class:`PromptDumper`（逐 tick 把
prompt / 模型 response 落盘）。开关在 ``kindred.yaml`` 的 ``debug.dump_enabled``
与 ``logging.level``。

⚠️ 区分边界（别再混）：本仓说「可观测 / observability」时，指的就是**本文件这一套
（心侧 / Python）**。它与 openclaw 平台侧的 diagnostics（prometheus / otel——那是
嘴 gateway 的运行指标、Node 进程、另一套系统）**是两回事**，平台 diagnostics 不属于
Kindred 可观测，不要互相代换。
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from kindred.config import DEFAULT_DEBUG_DUMP_DIR, DEFAULT_LOG_LEVEL

_OBSERVABILITY_SECRET_KEY_PARTS = (
    "apikey",
    "api_key",
    "authorization",
    "cookie",
    "device_id",
    "oauth",
    "password",
    "secret",
    "token",
    "xsec",
)
_OBSERVABILITY_REDACTED = "[redacted]"
_QUIET_THIRD_PARTY_LOGGERS = ("httpcore", "httpx")


@dataclass(frozen=True)
class DebugDumpConfig:
    """Debug dump controls.

    Debug dumps intentionally contain sensitive local context such as prompt,
    SOUL, and chat snippets. They are disabled by default and should only be
    enabled for local debugging.
    """

    enabled: bool = False
    dump_dir: Path = DEFAULT_DEBUG_DUMP_DIR
    max_files: int = 200

    def __post_init__(self) -> None:
        if self.max_files <= 0:
            raise ValueError("debug dump max_files must be positive")


def configure_logging(
    level: str = DEFAULT_LOG_LEVEL,
    *,
    log_file: Path | None = None,
    file_max_bytes: int = 10 * 1024 * 1024,
    file_backup_count: int = 5,
) -> None:
    """Configure process logging with Kindred's compact default format.

    ``log_file`` 给定时（daemon 常驻），额外挂一个滚动文件 handler——常驻后
    stderr 无人看，文件是唯一诊断入口（docs/16 §2.3）。文件 handler 失败（目录
    建不了 / 权限）**降级为只 stderr，不 raise**——日志落盘失败不该拖垮 daemon 启动。
    """

    numeric_level = _numeric_log_level(level)
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(numeric_level)
    else:
        logging.basicConfig(level=numeric_level, format=fmt)
    logging.getLogger("kindred").setLevel(numeric_level)
    # httpx/httpcore 的 DEBUG 会输出完整请求 URL；地图签名等凭据可能位于 query 中。
    # 即使 Kindred 开启 DEBUG，也不能放开这两个第三方 logger。
    for logger_name in _QUIET_THIRD_PARTY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    if log_file is not None:
        _attach_file_handler(
            root,
            log_file,
            numeric_level,
            fmt,
            max_bytes=file_max_bytes,
            backup_count=file_backup_count,
        )


def _attach_file_handler(
    root: logging.Logger,
    log_file: Path,
    numeric_level: int,
    fmt: str,
    *,
    max_bytes: int,
    backup_count: int,
) -> None:
    """挂滚动文件 handler；同一路径重复配置时复用并更新参数。

    失败（mkdir / 打开文件报错）降级为 warning + 只 stderr，不 raise。
    """
    resolved = str(log_file.resolve())
    for handler in root.handlers:
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename == resolved:
            handler.maxBytes = max_bytes
            handler.backupCount = backup_count
            handler.setLevel(numeric_level)
            handler.setFormatter(logging.Formatter(fmt))
            return
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "daemon log file 挂载失败，降级只 stderr：%s（%s）", log_file, exc
        )
        return
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(file_handler)


class PromptDumper:
    """Writes prompt/parsed-response artifacts only when explicitly enabled."""

    def __init__(self, config: DebugDumpConfig) -> None:
        self._config = config
        if self.enabled:
            self._config.dump_dir.mkdir(parents=True, exist_ok=True)
            logging.getLogger(__name__).warning(
                "Kindred debug dump enabled; artifacts in %s may contain SOUL, "
                "dialogue, prompts, and model responses.",
                self._config.dump_dir,
            )

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def dump(
        self,
        *,
        role: str,
        prompt: str,
        response: Mapping[str, Any] | None,
        tick_id: int | str | None = None,
        triggered_at: str | None = None,
        raw_text: str | None = None,
    ) -> Path | None:
        """Write one markdown artifact and return its path, or ``None`` if gated off.

        ``raw_text``（earlier review review N-1 / step 2）：解析失败时模型的**原始未解析文本**。
        给定时单独渲染一段「Raw Response (unparsed)」——这是把失败全文落进**本 gated +
        0600** 路径的唯一出口（client 异常只带不含正文的指纹，绝不在常态日志里带全文）。
        """

        if not self.enabled:
            return None

        ts = triggered_at or datetime.now(tz=timezone.utc).isoformat()
        tick_part = _safe_component(str(tick_id)) if tick_id is not None else "noid"
        role_part = _safe_component(role)
        filename = f"tick-{_safe_component(ts)}-{tick_part}-{role_part}.md"
        path = self._config.dump_dir / filename

        content = _render_dump(
            role=role,
            prompt=prompt,
            response=response,
            tick_id=tick_id,
            triggered_at=triggered_at,
            raw_text=raw_text,
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
        finally:
            try:
                path.chmod(0o600)
            except OSError:
                pass
        _prune_debug_dumps(self._config.dump_dir, self._config.max_files)
        return path


def _prune_debug_dumps(dump_dir: Path, max_files: int) -> None:
    """删除最旧的 Kindred tick dump；清理失败不影响当前 tick。"""

    try:
        artifacts = sorted(
            dump_dir.glob("tick-*.md"),
            key=lambda item: (item.stat().st_mtime_ns, item.name),
        )
        for artifact in artifacts[:-max_files]:
            artifact.unlink()
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Kindred debug dump 清理失败；保留现有文件继续运行：%s", type(exc).__name__
        )


def redact_observability_secrets(value: Any) -> Any:
    """Redact token/capability-like values before observability artifacts persist.

    这个函数守住的是「可观测出口」边界：debug dump / tool_trace / 后续 RESUME 派生
    都可能接到模型复述或上游异常塞进来的 token-like 内容，所以不能依赖调用点逐字段
    手工挑。这里递归处理任意嵌套结构，key 命中或 value 看起来像 capability URL /
    token 时统一替换成短占位符。
    """

    if isinstance(value, dict):
        return {
            key: (
                _OBSERVABILITY_REDACTED
                if _is_observability_secret_key(key)
                else redact_observability_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_observability_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_observability_secrets(item) for item in value)
    if isinstance(value, str) and _is_observability_secret_value(value):
        return _OBSERVABILITY_REDACTED
    return deepcopy(value)


def _is_observability_secret_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.replace("-", "_").lower()
    return any(part in normalized for part in _OBSERVABILITY_SECRET_KEY_PARTS)


def _is_observability_secret_value(value: str) -> bool:
    normalized = value.lower()
    if "xsec" in normalized or "cookie" in normalized:
        return True
    return bool(
        re.search(
            r"(?:\?|&)[^=\s]*(?:token|secret|oauth|authorization|cookie|xsec)[^=\s]*=",
            normalized,
        )
    )


def _numeric_log_level(level: str) -> int:
    numeric = logging.getLevelName(level.strip().upper())
    if not isinstance(numeric, int):
        raise ValueError(f"unknown log level: {level!r}")
    return numeric


def _safe_component(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value)


def _render_dump(
    *,
    role: str,
    prompt: str,
    response: Mapping[str, Any] | None,
    tick_id: int | str | None,
    triggered_at: str | None,
    raw_text: str | None = None,
) -> str:
    response_dict = dict(redact_observability_secrets(response or {}))
    safe_raw_text = redact_observability_secrets(raw_text) if raw_text is not None else None
    act_decision = response_dict.get("act_decision")
    note = response_dict.get("note")
    lines = [
        f"# Kindred Debug Dump: {role}",
        "",
        f"- tick_id: {tick_id if tick_id is not None else 'noid'}",
        f"- triggered_at: {triggered_at or 'unknown'}",
        "",
        "## Prompt",
        "",
        "```text",
        prompt,
        "```",
        "",
        "## Parsed Response",
        "",
        "```json",
        json.dumps(response_dict, ensure_ascii=False, indent=2, default=str),
        "```",
        "",
    ]
    # 解析失败时无 parsed response——把模型原始未解析文本落进本 0600 工件（唯一全文出口）。
    if safe_raw_text is not None:
        lines += [
            "## Raw Response (unparsed)",
            "",
            "```text",
            safe_raw_text,
            "```",
            "",
        ]
    lines += [
        "## Extracted Fields",
        "",
        "```json",
        json.dumps(
            {"act_decision": act_decision, "note": note},
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        "```",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "DebugDumpConfig",
    "PromptDumper",
    "configure_logging",
    "redact_observability_secrets",
]
