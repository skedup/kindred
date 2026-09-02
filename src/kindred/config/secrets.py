"""Kindred 私有凭据文件的窄解析与运行期注入。"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping, MutableMapping
from pathlib import Path

SUPPORTED_SECRET_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "BAIDU_MAP_AK",
        "BAIDU_MAP_SK",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "KINDRED_CAPABILITY_DRAW_API_KEY",
        "KINDRED_GATEWAY_TOKEN",
        "OPENAI_API_KEY",
        "XAI_API_KEY",
    }
)


class KindredSecretError(ValueError):
    """凭据输入、文件类型或权限不符合公开运行合同。"""


def validate_secret_values(values: Mapping[str, str]) -> dict[str, str]:
    """校验内存凭据；错误只暴露 key，不暴露 value。"""
    result: dict[str, str] = {}
    for key, value in values.items():
        if key not in SUPPORTED_SECRET_KEYS:
            raise KindredSecretError(f"unknown secret key: {key}")
        if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
            raise KindredSecretError(f"invalid secret value: {key}")
        result[key] = value
    return result


def serialize_secrets(values: Mapping[str, str]) -> str:
    """按稳定顺序生成数据文件；该格式不是 shell 脚本。"""
    validated = validate_secret_values(values)
    return "".join(f"{key}={value}\n" for key, value in sorted(validated.items()))


def read_secrets_file(path: Path) -> dict[str, str]:
    """读取 ``KEY=value`` 数据文件，拒绝 symlink、宽权限与模糊语法。"""
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise KindredSecretError("secrets file must be a regular file")
        if info.st_uid != os.getuid():
            raise KindredSecretError("secrets file must be owned by the current user")
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise KindredSecretError("secrets file permissions are too permissive")
        with path.open("r", encoding="utf-8", newline="") as handle:
            text = handle.read()
    except OSError as exc:
        raise KindredSecretError("secrets file is unavailable") from exc

    values: dict[str, str] = {}
    for line_number, line in enumerate(text.split("\n"), start=1):
        if not line:
            continue
        if "=" not in line:
            raise KindredSecretError(f"invalid secrets line: {line_number}")
        key, value = line.split("=", 1)
        if key in values:
            raise KindredSecretError(f"duplicate secret key: {key}")
        values[key] = value
    return validate_secret_values(values)


def merge_runtime_secrets(
    path: Path | None,
    *,
    env: Mapping[str, str],
) -> dict[str, str]:
    """返回 ``file < process env`` 的凭据视图，不修改调用方环境。"""
    merged = read_secrets_file(path) if path is not None else {}
    for key in SUPPORTED_SECRET_KEYS:
        value = env.get(key)
        if value:
            merged[key] = value
    return merged


def install_runtime_secrets(
    path: Path | None,
    *,
    env: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    """把文件凭据补入进程环境；已存在环境变量永远优先。"""
    target = os.environ if env is None else env
    values = merge_runtime_secrets(path, env=target)
    for key, value in values.items():
        target.setdefault(key, value)
    return tuple(sorted(values))


__all__ = [
    "KindredSecretError",
    "SUPPORTED_SECRET_KEYS",
    "install_runtime_secrets",
    "merge_runtime_secrets",
    "read_secrets_file",
    "serialize_secrets",
    "validate_secret_values",
]
